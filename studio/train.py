"""视频一键训练角色 LoRA：抽帧(1fps)→本地筛选→本地打标→dataset→ai-toolkit 训练。全部在本机，不回传画面。"""
import asyncio, re, shutil, subprocess, time
from pathlib import Path
from aiohttp import web
import video

T = Path('/root/autodl-tmp/train'); DS = T/'dataset'; OUT = T/'output'; CFG = T/'configs'
STEP = re.compile(r'(\d+)/(\d+) \[')

def _ready():
    miss = []
    if not (T/'models/diffusion_models/qwen_image_2.1_bf16.safetensors').is_file(): miss.append('底模还没下完')
    if not (T/'venv/bin/python').is_file() or not (T/'INSTALL_OK').is_file(): miss.append('训练环境还没装完')
    return miss

BACKEND = 'http://127.0.0.1:8192'
def busy(app):
    return

async def gpu_gate(app):
    """开训前：素材包/单张在跑就拒绝；否则让 ComfyUI 卸载模型、释放显存。返回说明文字。"""
    cur = app.get('train_state')
    if cur and cur.get('state') == 'running': raise ValueError('已有训练在进行')
    miss = _ready()
    if miss: raise ValueError('还不能训练：' + '；'.join(miss))
    p = app.get('pack_state')
    if p and p.get('state') == 'running': raise ValueError('素材包正在生成，显存被占着。请先等它跑完或点「停止」，再开训练。')
    if any(j.get('state') in ('queued', 'running') for j in app.get('jobs', {}).values()): raise ValueError('单张生成还在进行，请等它出完再开训练。')
    def used():
        try:
            o = subprocess.run(['nvidia-smi', '--query-gpu=memory.used,memory.total', '--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=20).stdout
            a, b = o.strip().split('\n')[0].split(','); return int(a), int(b)
        except Exception: return None
    before = await asyncio.to_thread(used)
    try:
        async with app['http'].post(BACKEND + '/free', json={'unload_models': True, 'free_memory': True}) as r: ok = r.status == 200
    except Exception as e: ok = False
    await asyncio.sleep(4)
    after = await asyncio.to_thread(used)
    note = ('已让 ComfyUI 释放显存' if ok else 'ComfyUI 释放显存接口没响应（可能没开），直接开训') + \
           (f'：{before[0]//1024}GB → {after[0]//1024}GB / {after[1]//1024}GB' if before and after else '')
    return note

def _caption(trigger):
    # 本地固定打标：触发词 + 通用描述（不调用任何外部服务）
    return f'{trigger}, a photo of a person'

_EN = {'正面': 'front view', '侧前': 'three-quarter view', '侧面': 'side profile view', '背面': 'back view',
       '头肩': 'close-up portrait', '半身': 'upper body', '全身': 'full body',
       '平静': 'neutral expression', '微笑': 'smiling', '大笑': 'laughing', '生气': 'angry', '难过': 'sad', '惊讶': 'surprised', '喊叫': 'shouting'}
IMG = ('.jpg', '.jpeg', '.png', '.webp')
def _folder(name):
    if name == '_all_photos': return name
    if not isinstance(name, str) or not name or '/' in name or '\\' in name or name.startswith('.'): raise ValueError('图片文件夹无效')
    f = video.INPUTS/name
    if not f.is_dir(): raise ValueError('找不到图片文件夹：' + name)
    return name

OUTPUTS = Path('/root/autodl-tmp/outputs')
_SEG = re.compile(r'[^/\\\x00]+\Z')
def _picked(items):
    """勾选的单张图：[{src:'lib',name:'子文件夹/x.png'} | {src:'batch',batch:'...',name:'x.png'}] → 绝对路径列表（只校验路径，不读内容）"""
    if not isinstance(items, list) or not items: raise ValueError('没有勾选图片')
    if len(items) > 500: raise ValueError('一次最多勾选 500 张')
    out = []
    for it in items:
        name = str((it or {}).get('name') or '')
        parts = name.split('/')
        if not name or any(not _SEG.fullmatch(x) or x.startswith('.') for x in parts) or Path(name).suffix.lower() not in IMG:
            raise ValueError('图片名无效：' + name[:60])
        if it.get('src') == 'batch':
            b = str(it.get('batch') or '')
            if not _SEG.fullmatch(b) or b.startswith('.') or len(parts) != 1: raise ValueError('批次无效')
            f = OUTPUTS/b/name
        else:
            if len(parts) > 2: raise ValueError('图片路径无效')
            f = video.INPUTS/name
        if not f.is_file(): raise ValueError('找不到图片：' + name[:60])
        if f not in out: out.append(f)
    if len(out) < 10: raise ValueError(f'只勾选了 {len(out)} 张，至少要 10 张才能训练')
    return out

def _prepare_files_sync(st, files, trigger):
    run = time.strftime('%Y%m%d-%H%M%S') + '_picked'
    ds = DS/run; ds.mkdir(parents=True)
    st['phase'] = '整理图片'; n = 0
    for i, f in enumerate(files):
        dst = f'{i+1:03d}_{f.name}'   # 不同批次可能同名，加序号避免覆盖
        shutil.copy2(f, ds/dst); (ds/(Path(dst).stem + '.txt')).write_text(_caption(trigger)); n += 1
    if n < 10: raise RuntimeError(f'只有 {n} 张，至少要 10 张才能训练')
    return _write_cfg(st, run, ds, n)

def _write_cfg(st, run, ds, n):
    trigger = st['trigger']
    steps = st.get('steps_override') or max(1500, min(3000, n * 60))
    steps = max(50, min(5000, int(steps)))
    sample_every = int(st.get('sample_every') or 100)
    sample_every = max(10, min(steps, sample_every))
    CFG.mkdir(exist_ok=True)
    y = (T/'lora_template.yaml').read_text().replace('__NAME__', 'lora_' + run).replace('__TRIGGER__', trigger)\
        .replace('__DATASET__', str(ds)).replace('__STEPS__', str(steps)).replace('__SAMPLE_EVERY__', str(sample_every))
    cfg = CFG/f'{run}.yaml'; cfg.write_text(y)
    st.update(run=run, images=n, steps_total=steps, sample_every=sample_every)
    return cfg

def _prepare_sync(st, vid, person, trigger, folder=None):
    if folder is None: folder = video._extract_sync(st, vid, person)
    src = video.INPUTS if folder == '_all_photos' else video.INPUTS/folder
    run = time.strftime('%Y%m%d-%H%M%S') + '_' + re.sub(r'[^A-Za-z0-9_]', '_', Path(vid or folder).stem)[:40]
    ds = DS/run; ds.mkdir(parents=True)
    import tagger
    tags = tagger.load(src) if folder != '_all_photos' else None
    if tags is None and folder != '_all_photos':
        try: st['phase'] = '智能分类中'; tags = tagger.tag_folder(src, st)
        except Exception as e: st['tag_error'] = str(e)[:200]; tags = None
    if tags:
        names = tagger.balanced(tags, 80); st['balanced'] = True
        files = [src/x for x in names if (src/x).is_file()]
        st['coverage_missing'] = tagger.coverage(tags)['missing']
    else:
        files = sorted(x for x in src.iterdir() if x.suffix.lower() in IMG)
    st['phase'] = '整理图片'
    n = 0
    for f in files:
        t = tags.get(f.name, {}) if tags else {}
        extra = ', '.join(x for x in (_EN.get(t.get('view')), _EN.get(t.get('framing')), _EN.get(t.get('expr'))) if x)
        shutil.copy2(f, ds/f.name); (ds/(f.stem + '.txt')).write_text(_caption(trigger) + (', ' + extra if extra else '')); n += 1
    if n < 10: raise RuntimeError(f'筛选后只剩 {n} 张，至少要 10 张才能训练（换更长/更清楚的视频）')
    return _write_cfg(st, run, ds, n)

def _train_sync(st, cfg):
    log = T/'logs'; log.mkdir(exist_ok=True); lf = open(log/f"{st['run']}.log", 'w')
    st.update(phase='训练中', log=str(log/f"{st['run']}.log"))
    p = subprocess.Popen([str(T/'train_run.sh'), str(cfg)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    st['_p'] = p; tail = []
    for line in p.stdout:
        lf.write(line); lf.flush()
        for part in line.split('\r'):
            m = STEP.search(part)
            if m and int(m.group(2)) == st.get('steps_total'):
                k = int(m.group(1)); now = time.time()
                if k != st.get('step'):
                    if '_t0' not in st: st['_t0'] = (k, now)
                    elif st.get('_tl'):
                        lk, lt = st['_tl']; d = (now - lt) / max(1, k - lk)
                        st['last_step_s'] = round(d, 2)
                        k0, t0 = st['_t0']
                        if k > k0: st['sec_per_step'] = round((now - t0) / (k - k0), 2); st['eta_s'] = round(st['sec_per_step'] * (st['steps_total'] - k))
                    st['_tl'] = (k, now)
                st['step'] = k
        tail = (tail + [line.strip()[-200:]])[-8:]
    p.wait(); lf.close()
    if st.get('stop'): return
    if p.returncode != 0: raise RuntimeError('训练出错：' + ' | '.join(x for x in tail if x)[-600:])
    outs = sorted((OUT/('lora_' + st['run'])).glob('*.safetensors'))
    st['outputs'] = [o.name for o in outs]

async def start(request):
    app = request.app; d = await request.json()
    cur = app.get('train_state')
    if cur and cur['state'] == 'running': raise ValueError('已有训练在进行')
    gnote = await gpu_gate(app)
    reuse = d.get('reuse_dataset')
    picked = _picked(d['files']) if d.get('files') else None
    folder = _folder(d['folder']) if (d.get('folder') and not picked and not reuse) else None
    vid = None if (folder or picked or reuse) else video._vname(d.get('video'))
    trigger = re.sub(r'[^a-z0-9]', '', str(d.get('trigger') or 'ohwxchar').lower())[:20] or 'ohwxchar'
    st = {'state': 'running', 'phase': '整理图片' if (folder or picked or reuse) else '抽帧中', 'video': vid, 'folder': folder, 'picked': len(picked) if picked else 0, 'trigger': trigger, 'extracted': 0, 'checked': 0, 'kept': 0,
          'blur': 0, 'dup': 0, 'noperson': 0, 'step': 0, 'steps_total': None, 'error': None, 'stop': False, 'started': time.time(), 'gpu_note': gnote}
    if d.get('steps') is not None:
        try: st['steps_override'] = int(d['steps'])
        except Exception: pass
    if d.get('sample_every') is not None:
        try: st['sample_every'] = int(d['sample_every'])
        except Exception: pass
    app['train_state'] = st
    async def run():
        try:
            if reuse:
                ds, n = _reuse_ds(reuse)
                run_id = time.strftime('%Y%m%d-%H%M%S') + '_reuse'
                cfg = await asyncio.to_thread(_write_cfg, st, run_id, ds, n)
            elif picked: cfg = await asyncio.to_thread(_prepare_files_sync, st, picked, trigger)
            else: cfg = await asyncio.to_thread(_prepare_sync, st, vid, d.get('person', True) is not False, trigger, folder)
            if st['stop']: st['state'] = 'stopped'; return
            await asyncio.to_thread(_train_sync, st, cfg)
            st['state'] = 'stopped' if st['stop'] else 'done'; st['phase'] = '完成' if st['state'] == 'done' else '已停止'
        except Exception as e:
            st.update(state='error', error=str(e)[:800])
    t = asyncio.create_task(run()); app['tasks'].add(t); t.add_done_callback(app['tasks'].discard)
    return web.json_response({'ok': True})

async def datasets(request):
    """已有训练包/数据集：只返回文件夹名、时间、图片张数、是否训练过，不读图片和标注。"""
    out = []
    zips = {z.name.split('_2')[0] if False else z.stem for z in (T/'packs').glob('*.zip')} if (T/'packs').is_dir() else set()
    for ds in sorted((p for p in DS.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True) if DS.is_dir() else []:
        if not _RUN_OK.fullmatch(ds.name): continue
        n = sum(1 for x in ds.iterdir() if x.suffix.lower() in IMG)
        if n < 10: continue
        trained = [p.name[len('lora_'):] for p in OUT.glob('lora_*') if p.is_dir() and any(p.glob('*.safetensors'))
                   and (CFG/(p.name[len('lora_'):] + '.yaml')).is_file() and str(ds) in (CFG/(p.name[len('lora_'):] + '.yaml')).read_text()]
        src = 'pack' if any(z.startswith(ds.name) for z in zips) else ('picked' if ds.name.endswith('_picked') else 'dataset')
        out.append({'name': ds.name, 'images': n, 'time': time.strftime('%m-%d %H:%M', time.localtime(ds.stat().st_mtime)),
                    'source': src, 'trained': len(trained)})
    return web.json_response({'datasets': out})

async def folders(request):
    out = []
    top = sum(1 for x in video.INPUTS.iterdir() if x.is_file() and x.suffix.lower() in IMG) if video.INPUTS.is_dir() else 0
    if top: out.append({'name': '_all_photos', 'count': top})
    for f in sorted(video.INPUTS.iterdir()) if video.INPUTS.is_dir() else []:
        if f.is_dir() and not f.name.startswith('.'):
            n = sum(1 for x in f.iterdir() if x.suffix.lower() in IMG)
            if n: out.append({'name': f.name, 'count': n})
    return web.json_response({'folders': out})

async def status(request):
    s = request.app.get('train_state')
    body = {'ready_missing': _ready()}
    if s: body |= {k: v for k, v in s.items() if not k.startswith('_') and k != 'stop'} | {'elapsed_s': round(time.time() - s['started'])}
    else: body['state'] = 'idle'
    return web.json_response(body)

async def stop(request):
    s = request.app.get('train_state')
    if s and s['state'] == 'running':
        s['stop'] = True; p = s.get('_p')
        if p and p.poll() is None: p.terminate()
    return web.json_response({'ok': True})

_RUN_OK = re.compile(r'^[A-Za-z0-9_\-]{1,80}$')
_IMG_NAME = re.compile(r'^[A-Za-z0-9_.\-]{1,200}$')

def _out_run(run):
    if not run or not _RUN_OK.fullmatch(run): raise ValueError('训练轮次无效')
    d = (OUT / ('lora_' + run)).resolve()
    if not str(d).startswith(str(OUT.resolve())): raise ValueError('路径无效')
    return d

def _samples_dir(run):
    return _out_run(run) / 'samples'

def _list_samples(run):
    d = _samples_dir(run)
    if not d.is_dir(): return []
    files = sorted((x for x in d.iterdir() if x.is_file() and x.suffix.lower() in ('.png', '.jpg', '.jpeg', '.webp')), key=lambda x: x.stat().st_mtime)
    return [f.name for f in files]

async def samples(request):
    """List training sample filenames/urls only — never prompt text."""
    s = request.app.get('train_state') or {}
    run = request.query.get('run') or s.get('run')
    if not run:
        runs = sorted((p for p in OUT.glob('lora_*') if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True)
        run = runs[0].name[len('lora_'):] if runs else None
    if not run:
        return web.json_response({'run': None, 'samples': [], 'count': 0})
    names = _list_samples(run)
    items = [{'name': n, 'url': f'/api/train/samples/{run}/{n}', 'thumb': f'/api/train/samples/{run}/{n}?thumb=1'} for n in names]
    return web.json_response({'run': run, 'samples': items, 'count': len(items), 'dir': f'train/output/lora_{run}/samples'})

async def sample_file(request):
    run = request.match_info['run']; name = request.match_info['name']
    if not _IMG_NAME.fullmatch(name): raise web.HTTPBadRequest(text='name')
    src = (_samples_dir(run) / name).resolve()
    root = _samples_dir(run).resolve()
    if not str(src).startswith(str(root)) or not src.is_file(): raise web.HTTPNotFound()
    import thumbs
    if request.query.get('thumb') == '1':
        return await thumbs.thumb_response(request, src, f'train-sample-{run}', name)
    return await thumbs.file_response(request, src, max_age=120)

def _reuse_ds(name):
    if not isinstance(name, str) or not _RUN_OK.fullmatch(name): raise ValueError('数据集无效')
    ds = (DS / name).resolve()
    if not str(ds).startswith(str(DS.resolve())) or not ds.is_dir(): raise ValueError('找不到数据集：' + name)
    n = sum(1 for x in ds.iterdir() if x.suffix.lower() in IMG)
    if n < 10: raise ValueError(f'数据集只有 {n} 张')
    return ds, n


def _adopt_running(app):
    """Reattach UI to an already-running run.py after studio soft-restart."""
    import os, subprocess as _sp
    try:
        out = _sp.run(['pgrep', '-af', 'run.py /root/autodl-tmp/train/configs/'], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return
    for line in out.splitlines():
        if 'run.py' not in line or '/configs/' not in line:
            continue
        m = re.search(r'configs/([A-Za-z0-9_\-]+)\.yaml', line)
        if not m:
            continue
        run = m.group(1)
        cfg = CFG / (run + '.yaml')
        logf = T / 'logs' / (run + '.log')
        steps_total = sample_every = None
        trigger = 'ohwxchar'
        if cfg.is_file():
            txt = cfg.read_text()
            mm = re.search(r'(?m)^\s*steps:\s*(\d+)', txt)
            steps_total = int(mm.group(1)) if mm else None
            mm = re.search(r'sample_every:\s*(\d+)', txt)
            sample_every = int(mm.group(1)) if mm else None
            mm = re.search(r'trigger_word:\s*"([^"]+)"', txt)
            if mm: trigger = mm.group(1)
        step = 0
        if logf.is_file():
            try:
                for part in logf.read_text(errors='ignore').replace('\n', '\r').split('\r'):
                    m2 = STEP.search(part)
                    if m2 and (steps_total is None or int(m2.group(2)) == steps_total):
                        step = max(step, int(m2.group(1)))
            except Exception:
                pass
        try:
            pid = int(line.split(None, 1)[0])
        except Exception:
            pid = None
        class _P:
            def __init__(self, pid): self.pid = pid
            def poll(self):
                if not self.pid: return 1
                try: os.kill(self.pid, 0); return None
                except ProcessLookupError: return 1
                except Exception: return None
            def terminate(self):
                if self.pid:
                    try: os.kill(self.pid, 15)
                    except Exception: pass
        st = {
            'state': 'running', 'phase': '训练中', 'video': None, 'folder': None, 'picked': 0,
            'trigger': trigger, 'extracted': 0, 'checked': 0, 'kept': 0, 'blur': 0, 'dup': 0, 'noperson': 0,
            'step': step, 'steps_total': steps_total, 'error': None, 'stop': False,
            'started': time.time(), 'gpu_note': '工作台重启后已接回训练（未中断）',
            'run': run, 'sample_every': sample_every, 'log': str(logf) if logf.is_file() else None,
            'adopted': True, '_p': _P(pid),
        }
        app['train_state'] = st
        def _follow_sync():
            while not st.get('stop'):
                p = st.get('_p')
                alive = p is None or p.poll() is None
                try:
                    if logf.is_file():
                        data = logf.read_text(errors='ignore').replace('\n', '\r')
                        for part in data.split('\r'):
                            m2 = STEP.search(part)
                            if m2 and (steps_total is None or int(m2.group(2)) == steps_total):
                                st['step'] = int(m2.group(1))
                except Exception:
                    pass
                if not alive:
                    outs = sorted((OUT / ('lora_' + run)).glob('*.safetensors'))
                    st['outputs'] = [o.name for o in outs]
                    st['state'] = 'stopped' if st.get('stop') else 'done'
                    st['phase'] = '已停止' if st.get('stop') else '完成'
                    return
                time.sleep(3)
        async def _on_start(app):
            task = asyncio.create_task(asyncio.to_thread(_follow_sync))
            app['tasks'].add(task)
            task.add_done_callback(app['tasks'].discard)
        app.on_startup.append(_on_start)
        break


def setup(app):
    _adopt_running(app)
    app.add_routes([web.post('/api/train', start), web.get('/api/train', status), web.post('/api/train/stop', stop), web.get('/api/train/folders', folders), web.get('/api/train/datasets', datasets),
                    web.get('/api/train/samples', samples), web.get('/api/train/samples/{run}/{name}', sample_file)])
