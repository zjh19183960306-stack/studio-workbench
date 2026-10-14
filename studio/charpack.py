"""charpack-v1 训练包：分片续传 .zip → 安全解包 → 校验 manifest → 补标注 → 生成训练配置 →（确认后）开训。
只校验结构/数量/文件名，不查看、不回传任何画面内容。任何文件都不删除（上传分片完成后改名为正式 zip 保留）。"""
import asyncio, hashlib, json, os, re, threading, time, zipfile
from pathlib import Path
from aiohttp import web
import train

T = train.T; PACKS = T/'packs'; PARTS = PACKS/'.parts'
MAX_PACK = 4 * 1024**3          # zip 本身最大 4GB
MAX_FILES = 3000                # zip 内条目上限
MAX_TOTAL = 6 * 1024**3         # 解压后总大小上限
MAX_ONE = 80 * 1024**2          # 单张图上限
MAX_CHUNK = 32 * 1024**2
ID = re.compile(r'[0-9a-f]{24}\Z')
IMG = ('.jpg', '.jpeg', '.png')
ENUM = {'angle': ('front', 'three_quarter', 'side', 'back'), 'framing': ('head', 'half', 'full'),
        'pitch': ('level', 'up', 'down'),
        'expression': ('neutral', 'smile', 'laugh', 'angry', 'sad', 'surprised', 'shout', 'unknown')}
EN = {'front': 'front view', 'three_quarter': 'three-quarter view', 'side': 'side profile view', 'back': 'view from behind',
      'head': 'close-up portrait', 'half': 'upper body shot', 'full': 'full body shot',
      'level': None, 'up': 'looking up', 'down': 'looking down',
      'neutral': 'neutral expression', 'smile': 'smiling', 'laugh': 'laughing', 'angry': 'angry expression',
      'sad': 'sad expression', 'surprised': 'surprised expression', 'shout': 'shouting', 'unknown': None}
_lock = threading.Lock(); _ups = {}

# ---------- 分片续传（与 chunkup.py 同协议：POST init → PUT ?offset= → POST finish） ----------
def _mp(uid): return PARTS/(uid + '.json')
def _pp(uid): return PARTS/(uid + '.part')
def _save(m):
    t = _mp(m['id']).with_suffix('.tmp'); t.write_text(json.dumps({k: v for k, v in m.items() if not k.startswith('_')}, ensure_ascii=False)); os.replace(t, _mp(m['id']))
def _load(uid):
    if not ID.fullmatch(uid or ''): raise ValueError('上传编号无效')
    with _lock:
        if uid in _ups: return _ups[uid]
        try: m = json.loads(_mp(uid).read_text())
        except Exception: raise ValueError('找不到这个上传，请重新选择训练包')
        if _pp(uid).is_file(): m['received'] = _pp(uid).stat().st_size
        _ups[uid] = m; return m

def init_sync(name, size, last_mod='', owner=''):
    raw = re.sub(r'[\x00-\x1f/\\]', '_', Path(str(name or '')).name).lstrip('.')
    if not raw.lower().endswith('.zip'): raise ValueError('训练包必须是 .zip')
    size = int(size or 0)
    if not 0 < size <= MAX_PACK: raise ValueError('训练包大小无效（最大 4 GB）')
    uid = hashlib.sha1(f'{raw}|{size}|{last_mod}|{owner}'.encode()).hexdigest()[:24]
    PARTS.mkdir(parents=True, exist_ok=True)
    try: m = _load(uid)
    except ValueError: m = None
    if not m or m.get('state') not in ('uploading', 'done'):
        m = {'id': uid, 'name': raw[:120], 'size': size, 'received': 0, 'state': 'uploading', 'created': time.time()}
        with _lock: _ups[uid] = m
        _pp(uid).touch(); _save(m)
    if m['state'] == 'uploading': m['received'] = _pp(uid).stat().st_size
    m['touched'] = time.time()
    return m

def finish_sync(uid):
    m = _load(uid)
    if m.get('state') == 'done': return m
    have = _pp(uid).stat().st_size
    if have != m['size']: raise ValueError(f'还没传完（{have}/{m["size"]}）')
    PACKS.mkdir(parents=True, exist_ok=True)
    dest = PACKS/(time.strftime('%Y%m%d-%H%M%S') + '_' + m['name'])
    os.replace(_pp(uid), dest)          # 改名保留，不删除
    m.update(state='done', zip=str(dest), finished=time.time()); _save(m)
    return m

# ---------- 安全解包 + 校验 ----------
_SEG = re.compile(r'[A-Za-z0-9._\- \u4e00-\u9fff()]+\Z')
def _safe_member(n):
    """只接受 manifest.json 和 images/<文件名>（允许 zip 外面多包一层同名目录）。返回 (kind, basename) 或 None"""
    if '\\' in n or n.startswith('/') or '\x00' in n or re.match(r'[A-Za-z]:', n): raise ValueError('训练包里有非法路径：' + n[:80])
    parts = [p for p in n.split('/') if p]
    if any(p in ('.', '..') for p in parts): raise ValueError('训练包里有非法路径（..）：' + n[:80])
    return parts

def unpack_sync(zpath, root, st):
    """返回 (ds_dir, manifest, warnings)。root = 数据集父目录。"""
    warn = []
    try: z = zipfile.ZipFile(zpath)
    except zipfile.BadZipFile: raise ValueError('不是有效的 zip 文件')
    with z:
        infos = z.infolist()
        if len(infos) > MAX_FILES: raise ValueError(f'训练包里文件太多（{len(infos)} > {MAX_FILES}）')
        total = 0; pick = {}; man = None; prefix = None; skipped = 0
        names = [i.filename for i in infos]
        if 'manifest.json' not in names:   # 允许外面套一层文件夹
            cand = [n for n in names if n.endswith('/manifest.json') and n.count('/') == 1]
            if len(cand) == 1: prefix = cand[0][:-len('manifest.json')]
            else: raise ValueError('训练包根目录没有 manifest.json')
        for i in infos:
            parts = _safe_member(i.filename)
            if i.is_dir() or not parts: continue
            if (i.external_attr >> 16) & 0o170000 == 0o120000: raise ValueError('训练包里不能有符号链接：' + i.filename[:80])
            rel = '/'.join(parts)
            if prefix:
                if not rel.startswith(prefix): skipped += 1; continue
                rel = rel[len(prefix):]
            if rel == 'manifest.json':
                if i.file_size > 4 * 1024**2: raise ValueError('manifest.json 过大')
                man = i; continue
            rp = rel.split('/')
            if len(rp) != 2 or rp[0] != 'images' or Path(rp[1]).suffix.lower() not in IMG + ('.txt',) or rp[1].startswith('.') or not _SEG.fullmatch(rp[1]):
                skipped += 1; continue
            lim = 64 * 1024 if rp[1].lower().endswith('.txt') else MAX_ONE
            if i.file_size > lim: raise ValueError(f'文件过大：{rel}')
            total += i.file_size
            if total > MAX_TOTAL: raise ValueError('训练包解压后超过 6 GB 上限')
            if rp[1].lower() in {k.lower() for k in pick}: raise ValueError('图片重名（大小写）：' + rp[1])
            pick[rp[1]] = i
        if skipped: warn.append(f'跳过了 {skipped} 个不认识的文件（只收 images/ 里的 jpg/png/txt）')
        try: manifest = json.loads(z.read(man).decode('utf-8-sig'))
        except Exception as e: raise ValueError('manifest.json 不是有效的 JSON：' + str(e)[:120])
        manifest, w2 = validate_manifest(manifest, {k for k in pick if Path(k).suffix.lower() in IMG})
        warn += w2
        imgs = [k for k in pick if Path(k).suffix.lower() in IMG]
        if len(imgs) < 10: raise ValueError(f'训练包里只有 {len(imgs)} 张图，至少要 10 张')
        run = time.strftime('%Y%m%d-%H%M%S') + '_' + manifest['name']
        ds = Path(root)/run; ds.mkdir(parents=True)
        st['phase'] = '解包中'; done = 0
        for k, i in pick.items():
            dst = (ds/k).resolve()
            if dst.parent != ds.resolve(): raise ValueError('非法路径：' + k)
            n = 0
            with z.open(i) as src, open(dst, 'xb') as out:   # 'x'：不覆盖已有文件
                while True:
                    b = src.read(1 << 20)
                    if not b: break
                    n += len(b)
                    if n > i.file_size or n > MAX_ONE: raise ValueError('解压大小与声明不符：' + k)
                    out.write(b)
            done += 1; st['unpacked'] = done
    # 图片完整性（只解码校验，不展示、不回传）
    from PIL import Image
    bad = []
    for k in imgs:
        try:
            with Image.open(ds/k) as im: im.verify()
        except Exception: bad.append(k)
    if bad: warn.append(f'{len(bad)} 张图片损坏，不参与训练：' + ', '.join(bad[:5]))
    good = [k for k in imgs if k not in bad]
    if len(good) < 10: raise ValueError(f'有效图片只有 {len(good)} 张，至少要 10 张')
    return ds, run, manifest, good, bad, warn

def validate_manifest(m, files):
    warn = []
    if not isinstance(m, dict): raise ValueError('manifest.json 必须是对象')
    if m.get('format') != 'charpack-v1': raise ValueError('manifest.format 必须是 "charpack-v1"')
    name = re.sub(r'[^A-Za-z0-9_\-]', '_', str(m.get('name') or ''))[:40].strip('_-')
    if not name: raise ValueError('manifest.name 必须是英文或拼音')
    trig = re.sub(r'[^a-z0-9]', '', str(m.get('trigger') or '').lower())[:20]
    lst = m.get('images')
    if not isinstance(lst, list) or not lst: raise ValueError('manifest.images 必须是非空列表')
    meta = {}
    for e in lst:
        if not isinstance(e, dict) or not isinstance(e.get('file'), str): raise ValueError('manifest.images 每项都要有 file')
        f = e['file']
        if not f.startswith('images/'): raise ValueError('manifest 里的 file 要以 images/ 开头：' + f[:60])
        b = f[len('images/'):]
        if b not in files: warn.append('manifest 列出但包里没有：' + f[:60]); continue
        t = {}
        for k, allowed in ENUM.items():
            v = e.get(k, 'unknown' if k == 'expression' else None)
            if v is not None and v not in allowed: warn.append(f'{b} 的 {k}="{str(v)[:20]}" 不认识，已忽略'); v = None
            t[k] = v
        s = e.get('sharpness')
        t['sharpness'] = s if isinstance(s, (int, float)) else None
        meta[b] = t
    extra = sorted(files - set(meta))
    if extra: warn.append(f'{len(extra)} 张图不在 manifest 里，按无标签处理')
    if len(warn) > 30: warn = warn[:30] + [f'……另有 {len(warn)-30} 条提示']
    cov = m.get('coverage') if isinstance(m.get('coverage'), dict) else None
    miss = [str(x)[:40] for x in m.get('missing')] if isinstance(m.get('missing'), list) else None
    return {'name': name, 'trigger': trig, 'meta': meta, 'coverage': cov, 'missing': miss}, warn

def _coverage(meta, good):
    c = {k: {v: 0 for v in vals} for k, vals in ENUM.items()}
    for f in good:
        for k in ENUM:
            v = (meta.get(f) or {}).get(k)
            if v: c[k][v] += 1
    miss = [f'{k}:{v}' for k in ('angle', 'framing') for v, n in c[k].items() if n == 0]
    return c, miss

def caption(trigger, t):
    bits = [trigger, 'a photo of a person'] + [EN[t[k]] for k in ('angle', 'framing', 'pitch', 'expression') if t.get(k) and EN.get(t[k])]
    return ', '.join(bits)

def prepare_sync(st, zpath, root, trigger_hint=None, cfg_dir=None):
    ds, run, man, good, bad, warn = unpack_sync(zpath, root, st)
    trigger = man['trigger'] or re.sub(r'[^a-z0-9]', '', str(trigger_hint or '').lower())[:20] or 'ohwxchar'
    st['phase'] = '生成标注'; made = 0; fixed = 0
    for f in good:
        txt = ds/(Path(f).stem + '.txt')
        if txt.is_file():
            s = txt.read_text(encoding='utf-8', errors='replace').strip()
            if trigger not in s.split(', ')[0:1] and not s.startswith(trigger):
                txt.write_text(trigger + ', ' + s, encoding='utf-8'); fixed += 1
        else:
            txt.write_text(caption(trigger, man['meta'].get(f) or {}), encoding='utf-8'); made += 1
    cov, miss = _coverage(man['meta'], good)
    st.update(trigger=trigger, captions_made=made, captions_fixed=fixed, coverage=cov,
              missing=man['missing'] if man['missing'] is not None else miss, missing_calc=miss,
              manifest_coverage=man['coverage'], warnings=warn, bad=len(bad), dataset=str(ds))
    st['phase'] = '生成训练配置'
    if cfg_dir: old = train.CFG; train.CFG = Path(cfg_dir)
    try: cfg = train._write_cfg(st, run, ds, len(good))
    finally:
        if cfg_dir: train.CFG = old
    st['cfg'] = str(cfg)
    return cfg

# ---------- 接口 ----------
async def api_init(request):
    d = await request.json()
    m = init_sync(d.get('name'), d.get('size'), d.get('lastModified') or '', request.get('owner') or '')
    return web.json_response({'id': m['id'], 'received': m['received'] if m['state'] == 'uploading' else m['size'], 'state': m['state']})

async def api_put(request):
    m = _load(request.match_info['uid'])
    if m.get('state') != 'uploading': raise ValueError('这个上传已经结束')
    off = int(request.query.get('offset', '-1')); part = _pp(m['id'])
    have = part.stat().st_size if part.is_file() else 0
    if off != have or m.get('busy'): return web.json_response({'error': '位置不一致', 'received': have}, status=409)
    m['busy'] = True; n = 0
    try:
        with open(part, 'ab') as f:
            async for c in request.content.iter_chunked(1 << 20):
                n += len(c)
                if n > MAX_CHUNK or have + n > m['size']: raise ValueError('分片过大')
                f.write(c)
    except (ValueError, asyncio.CancelledError, ConnectionResetError, OSError):
        with open(part, 'r+b') as f: f.truncate(have)
        raise
    finally: m['busy'] = False
    m['received'] = have + n; m['touched'] = time.time()
    return web.json_response({'received': m['received']})

async def api_finish(request):
    app = request.app; m = await asyncio.to_thread(finish_sync, request.match_info['uid'])
    cur = app.get('charpack_state')
    if cur and cur['state'] == 'running': raise ValueError('上一个训练包还在解包')
    d = {}
    try: d = await request.json()
    except Exception: pass
    st = {'state': 'running', 'phase': '解包中', 'zip': Path(m['zip']).name, 'started': time.time(), 'error': None}
    app['charpack_state'] = st
    async def run():
        try:
            await asyncio.to_thread(prepare_sync, st, m['zip'], train.DS, d.get('trigger'))
            st.update(state='ready', phase='已就绪，等确认开训')
        except Exception as e: st.update(state='error', error=str(e)[:600])
    t = asyncio.create_task(run()); app['tasks'].add(t); t.add_done_callback(app['tasks'].discard)
    return web.json_response({'ok': True, 'zip': st['zip']})

async def api_status(request):
    s = request.app.get('charpack_state') or {'state': 'idle'}
    return web.json_response({k: v for k, v in s.items() if k not in ('dataset', 'cfg')} | {'ready_missing': train._ready()})

async def api_train(request):
    app = request.app; s = app.get('charpack_state')
    if not s or s.get('state') != 'ready': raise ValueError('没有已就绪的训练包，请先上传')
    note = await train.gpu_gate(app)
    st = {'state': 'running', 'phase': '释放显存', 'video': None, 'folder': None, 'picked': 0, 'pack': s['zip'], 'trigger': s['trigger'],
          'run': s['run'], 'images': s['images'], 'steps_total': s['steps_total'], 'coverage_missing': s.get('missing'),
          'extracted': 0, 'checked': 0, 'kept': 0, 'blur': 0, 'dup': 0, 'noperson': 0, 'step': 0, 'error': None, 'stop': False, 'started': time.time(), 'gpu_note': note}
    app['train_state'] = st; s['state'] = 'training'; s['phase'] = '已开训'
    cfg = s['cfg']
    async def run():
        try:
            await asyncio.to_thread(train._train_sync, st, cfg)
            st['state'] = 'stopped' if st['stop'] else 'done'; st['phase'] = '完成' if st['state'] == 'done' else '已停止'
        except Exception as e: st.update(state='error', error=str(e)[:800])
    t = asyncio.create_task(run()); app['tasks'].add(t); t.add_done_callback(app['tasks'].discard)
    return web.json_response({'ok': True, 'freed': st.get('gpu_note')})

def setup(app):
    app.add_routes([web.post('/api/charpack/chunk', api_init), web.put('/api/charpack/chunk/{uid}', api_put),
                    web.post('/api/charpack/chunk/{uid}/finish', api_finish), web.get('/api/charpack', api_status),
                    web.post('/api/charpack/train', api_train)])
