"""分片/断点续传视频上传 + 边传边抽帧/筛选/智能分类 + 人物碎片素材库。
全部本机 CPU 处理（ffmpeg 低优先级、InsightFace/CLIP 走 CPU），不联网，不回传画面；只写帧文件和 tags.json。"""
import asyncio, hashlib, json, os, re, shutil, subprocess, threading, time
from pathlib import Path
from aiohttp import web
import video

R = video.R; VIDEOS = video.VIDEOS; INPUTS = video.INPUTS
PARTS = VIDEOS/'.parts'
MAX_CHUNK = 32 * 1024**2
ID = re.compile(r'[0-9a-f]{24}\Z')
_lock = threading.Lock()
_ups = {}          # id -> meta（内存里的活动上传）
MIN_START = 2 * 1024**2   # 收到 2MB 后开始判断能否边传边解

def _nice():
    try: os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 15)
    except Exception: pass

def _meta_path(uid): return PARTS/(uid + '.json')
def _part_path(uid): return PARTS/(uid + '.part')

def _save(m):
    p = _meta_path(m['id']); tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps({k: v for k, v in m.items() if not k.startswith('_')}, ensure_ascii=False)); os.replace(tmp, p)

def _load(uid):
    if not ID.fullmatch(uid or ''): raise ValueError('上传编号无效')
    with _lock:
        if uid in _ups: return _ups[uid]
        try: m = json.loads(_meta_path(uid).read_text())
        except Exception: raise ValueError('找不到这个上传，请重新选择视频')
        if _part_path(uid).is_file(): m['received'] = _part_path(uid).stat().st_size
        _ups[uid] = m; return m

def _clean_name(raw):
    raw = re.sub(r'[\x00-\x1f/\\]', '_', Path(str(raw or '')).name).lstrip('.')
    if not video.VID.fullmatch(raw): raise ValueError('只支持 mp4/mov/mkv/webm/m4v')
    stem, ext = raw.rsplit('.', 1); return stem[:120], ext

def _reserve(stem, ext):
    taken = {m.get('name') for m in _ups.values() if m.get('state') == 'uploading'}
    for n in range(10000):
        name = f'{stem}.{ext}' if n == 0 else f'{stem}_{n}.{ext}'
        if not (VIDEOS/name).exists() and name not in taken: return name
    raise ValueError('同名文件过多')

def active_uploads():
    with _lock: return [m for m in _ups.values() if m.get('state') == 'uploading' and time.time() - m.get('touched', 0) < 120]

# ---------- 判断 MP4/MOV 能否边传边解：顶层 box 里 moov 是否在 mdat 之前 ----------
def _mp4_layout(path, have):
    """返回 'front' | 'end' | 'unknown'（还需要更多数据）"""
    try:
        with open(path, 'rb') as f:
            pos = 0
            while pos + 8 <= have:
                f.seek(pos); h = f.read(16)
                if len(h) < 8: return 'unknown'
                size = int.from_bytes(h[:4], 'big'); typ = h[4:8]
                if size == 1:
                    if len(h) < 16: return 'unknown'
                    size = int.from_bytes(h[8:16], 'big')
                elif size == 0: size = 1 << 62
                if typ == b'moov' or typ == b'moof': return 'front'
                if typ == b'mdat': return 'end'
                if size < 8: return 'end'
                pos += size
    except Exception: pass
    return 'unknown'

# ---------- 抽帧后的筛选+分类（逐帧，增量写 tags.json） ----------
class Proc:
    def __init__(self, st, out, base, use_person):
        import cv2
        self.cv2 = cv2; self.st = st; self.out = out; self.base = base; self.use_person = use_person
        self.hog = cv2.HOGDescriptor(); self.hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
        self.casc = [cv2.CascadeClassifier(cv2.data.haarcascades + n) for n in
                     ('haarcascade_frontalface_default.xml', 'haarcascade_profileface.xml', 'haarcascade_upperbody.xml')]
        self.hashes = []; self.tags = {}
        try:
            import tagger; self.tagger = tagger; old = tagger.load(out)
            if old: self.tags.update(old)
        except Exception as e: self.tagger = None; st['tag_error'] = f'{type(e).__name__}:{str(e)[:200]}'
        self.last_write = 0
    def has_person(self, gray, bgr):
        for c in self.casc:
            if len(c.detectMultiScale(gray, 1.1, 5, minSize=(40, 40))): return True
        rects, _ = self.hog.detectMultiScale(bgr, winStride=(8, 8), scale=1.05)
        return len(rects) > 0
    def one(self, f):
        import imagehash
        from PIL import Image
        cv2 = self.cv2; st = self.st
        img = cv2.imread(str(f)); st['checked'] = st.get('checked', 0) + 1
        if img is None: f.unlink(missing_ok=True); return
        h, w = img.shape[:2]; s = 720 / max(h, w)
        small = cv2.resize(img, (int(w*s), int(h*s))) if s < 1 else img
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        drop = None
        if cv2.Laplacian(gray, cv2.CV_64F).var() < 60: drop = 'blur'
        else:
            ph = imagehash.phash(Image.fromarray(cv2.cvtColor(small, cv2.COLOR_BGR2RGB)))
            if any(ph - o <= 6 for o in self.hashes[-30:]): drop = 'dup'
            elif self.use_person and not self.has_person(gray, small): drop = 'noperson'
        if drop:
            st[drop] = st.get(drop, 0) + 1; f.unlink(missing_ok=True); return   # 只删自己刚抽出的临时帧
        self.hashes.append(ph)
        target = self.out/f'{self.base}_{f.stem[2:]}.jpg'
        shutil.move(str(f), target); st['kept'] = st.get('kept', 0) + 1
        if self.tagger:
            try: t = self.tagger.tag_one(target)
            except Exception as e: t = {'error': f'{type(e).__name__}:{str(e)[:120]}'}
            if t: self.tags[target.name] = t
            st['tagged'] = st.get('tagged', 0) + 1; st['tag_total'] = st['kept']
        if time.time() - self.last_write > 5: self.flush(False)
    def flush(self, links):
        p = self.out/'tags.json'; tmp = self.out/'.tags.json.tmp'
        tmp.write_text(json.dumps(self.tags, ensure_ascii=False, indent=0)); os.replace(tmp, p); self.last_write = time.time()
        if links and self.tagger:
            try: self.tagger._links(self.out, self.tags)
            except Exception as e: self.st['tag_error'] = str(e)[:200]

def _feeder(m, proc_stdin):
    """把不断增长的 .part 文件喂给 ffmpeg 的 stdin，直到上传完成。"""
    _nice(); part = _part_path(m['id']); pos = 0
    try:
        with open(part, 'rb') as f:
            while True:
                if m.get('_stop'): break
                have = part.stat().st_size if part.exists() else pos
                if have > pos:
                    f.seek(pos); b = f.read(min(have - pos, 4 << 20)); pos += len(b); proc_stdin.write(b); continue
                if m.get('state') in ('done', 'cancelled', 'error'):
                    fin = VIDEOS/m['name']
                    if m['state'] == 'done' and fin.is_file() and fin.stat().st_size > pos:   # 已改名，补齐尾部
                        with open(fin, 'rb') as g:
                            g.seek(pos)
                            while True:
                                b = g.read(4 << 20)
                                if not b: break
                                proc_stdin.write(b)
                    break
                time.sleep(0.5)
    except (BrokenPipeError, OSError): pass
    finally:
        try: proc_stdin.close()
        except Exception: pass

def _live_sync(app, m, st):
    _nice()
    ext = m['name'].rsplit('.', 1)[1].lower()
    out = video._dest(m['name']); tmp = R/'.frames_tmp'/(out.name + '_live')
    shutil.rmtree(tmp, ignore_errors=True); tmp.mkdir(parents=True); out.mkdir(parents=True, exist_ok=True)
    part = _part_path(m['id'])
    mode = None
    st.update(phase='等待数据（边传边抽）')
    while mode is None:
        if st['stop']: return
        have = part.stat().st_size if part.exists() else m.get('size', 0)
        if m.get('state') == 'done': have = m['size']
        if ext in ('mkv', 'webm'): mode = 'stream' if have >= MIN_START or m.get('state') == 'done' else None
        elif have >= min(MIN_START, m['size']) or m.get('state') == 'done':
            src = part if part.exists() else VIDEOS/m['name']
            lay = _mp4_layout(src, have)
            if lay == 'front': mode = 'stream'
            elif lay == 'end': mode = 'after'
            elif m.get('state') == 'done' or have >= 256 * 1024**2: mode = 'after'
        if m.get('state') in ('cancelled', 'error'): st.update(state='stopped', phase='上传已取消'); return
        if mode is None: time.sleep(1)
    st['live'] = mode == 'stream'
    if mode == 'after':
        st['live_reason'] = '这个视频的索引(moov)在文件末尾，必须整段传完才能解码，所以传完后再自动抽帧。用手机/剪映导出时选「快速启动/Web优化」可以边传边抽。'
        st.update(phase='等上传完成后再抽（原因见下）')
        while m.get('state') == 'uploading' and not st['stop']: time.sleep(1)
        if m.get('state') != 'done' or st['stop']:
            st.update(state='stopped' if st['stop'] else 'error', error=None if st['stop'] else '上传未完成'); return
    try:
        dur = float(subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', str(VIDEOS/m['name'])],
                                   capture_output=True, text=True, timeout=60).stdout.strip() or 0) if mode == 'after' else 0
    except Exception: dur = 0
    st.update(phase='边传边抽帧中' if mode == 'stream' else '抽帧中', total_est=int(dur) or None)
    cmd = ['nice', '-n', '15', 'ffmpeg', '-nostdin', '-v', 'error', '-threads', '4']
    cmd += ['-i', 'pipe:0'] if mode == 'stream' else ['-i', str(VIDEOS/m['name'])]
    cmd += ['-vf', 'fps=1', '-q:v', '2', str(tmp/'f_%06d.jpg')]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE if mode == 'stream' else subprocess.DEVNULL, stderr=subprocess.PIPE)
    if mode == 'stream':
        th = threading.Thread(target=_feeder, args=(m, p.stdin), daemon=True); th.start()
    errbuf = []
    threading.Thread(target=lambda: errbuf.append(p.stderr.read()[-600:]), daemon=True).start()
    proc = Proc(st, out, Path(m['name']).stem[:60], st.get('person_filter', True))
    done_names = set()
    def pending(final):
        fs = sorted(tmp.glob('f_*.jpg'))
        return fs if final else fs[:-1]   # 最后一张可能还没写完
    while True:
        if st['stop']:
            m['_stop'] = True; p.kill(); break
        alive = p.poll() is None
        fs = pending(not alive)
        st['extracted'] = len(done_names) + len(fs)
        for f in fs:
            if st['stop']: break
            done_names.add(f.name); proc.one(f)
        if not alive and not pending(True): break
        if not fs: time.sleep(1)
    proc.flush(True)
    shutil.rmtree(tmp, ignore_errors=True)
    if st['stop']: st['state'] = 'stopped'; return
    if p.returncode not in (0, None) and not st.get('kept'):
        st.update(state='error', error='ffmpeg 抽帧失败：' + (errbuf[0].decode('utf-8', 'replace') if errbuf and errbuf[0] else '格式可能不支持')[:240]); return
    if mode == 'stream' and p.returncode not in (0, None):
        st['live_note'] = '边传边解中途 ffmpeg 报错，已保留抽到的帧；可以在传完后再点一次「抽帧」补全。'
    st['state'] = 'done'; st['phase'] = '完成'

def _start_live(app, m, use_person):
    cur = app.get('extract_state')
    if cur and cur['state'] == 'running': return '已有抽帧/分类任务在进行，这个视频传完后请手动点「抽帧」'
    st = {'state': 'running', 'video': m['name'], 'phase': '准备', 'extracted': 0, 'checked': 0, 'kept': 0, 'blur': 0, 'dup': 0,
          'noperson': 0, 'person_filter': use_person, 'folder': video._dest(m['name']).name, 'error': None, 'stop': False,
          'started': time.time(), 'live': None, 'upload_id': m['id']}
    app['extract_state'] = st
    async def run():
        try: await asyncio.to_thread(_live_sync, app, m, st)
        except Exception as e: st.update(state='error', error=f'{type(e).__name__}:{str(e)[:280]}')
    t = asyncio.create_task(run()); app['tasks'].add(t); t.add_done_callback(app['tasks'].discard)
    return None

# ---------- 接口 ----------
async def init(request):
    d = await request.json(); stem, ext = _clean_name(d.get('name'))
    size = int(d.get('size') or 0)
    if not 0 < size <= video.MAX_VIDEO: raise ValueError('视频大小无效（最大 20 GB）')
    key = f"{stem}.{ext}|{size}|{d.get('lastModified') or ''}|{request.get('owner') or ''}"
    uid = hashlib.sha1(key.encode()).hexdigest()[:24]
    PARTS.mkdir(parents=True, exist_ok=True)
    live = d.get('live', True) is not False; person = d.get('person', True) is not False
    m = None
    try: m = _load(uid)
    except ValueError: pass
    if m and m.get('state') == 'done' and (VIDEOS/m['name']).is_file():
        return web.json_response({'id': uid, 'name': m['name'], 'received': size, 'state': 'done'})
    if not m or m.get('state') != 'uploading':
        with _lock:
            m = {'id': uid, 'name': _reserve(stem, ext), 'size': size, 'received': 0, 'state': 'uploading', 'created': time.time()}
            _ups[uid] = m
        _part_path(uid).touch(); _save(m)
    m['received'] = _part_path(uid).stat().st_size if _part_path(uid).is_file() else 0
    m['touched'] = time.time()
    msg = None
    cur = request.app.get('extract_state')
    if live and not (cur and cur.get('upload_id') == uid and cur['state'] == 'running'):
        msg = _start_live(request.app, m, person)
    return web.json_response({'id': uid, 'name': m['name'], 'received': m['received'], 'state': m['state'], 'live_msg': msg})

async def put_chunk(request):
    m = _load(request.match_info['uid'])
    if m.get('state') != 'uploading': raise ValueError('这个上传已经结束')
    off = int(request.query.get('offset', '-1')); part = _part_path(m['id'])
    have = part.stat().st_size if part.is_file() else 0
    if off != have: return web.json_response({'error': '位置不一致', 'received': have}, status=409)
    if 'busy' in m and m['busy']: return web.json_response({'error': '上一片还在写', 'received': have}, status=409)
    m['busy'] = True; n = 0
    try:
        with open(part, 'ab') as f:
            async for c in request.content.iter_chunked(1 << 20):
                n += len(c)
                if n > MAX_CHUNK or have + n > m['size']: raise ValueError('分片过大')
                f.write(c)
    except (ValueError, asyncio.CancelledError, ConnectionResetError, OSError):
        with open(part, 'r+b') as f: f.truncate(have)   # 半片作废，回到上一片末尾
        raise
    finally: m['busy'] = False
    m['received'] = have + n; m['touched'] = time.time()
    return web.json_response({'received': m['received']})

async def finish(request):
    m = _load(request.match_info['uid'])
    if m.get('state') == 'done': return web.json_response({'name': m['name'], 'mb': round(m['size']/1048576, 1)})
    part = _part_path(m['id']); have = part.stat().st_size
    if have != m['size']: raise ValueError(f'还没传完（{have}/{m["size"]}）')
    dest = VIDEOS/m['name']
    if dest.exists():
        with _lock: m['name'] = _reserve(*m['name'].rsplit('.', 1))
        dest = VIDEOS/m['name']
    os.link(part, dest)                      # 正式文件=同一份数据的硬链接，不复制不删除（.part 保留，同一 inode 不多占盘）
    m['state'] = 'done'; m['finished'] = time.time(); _save(m)
    return web.json_response({'name': m['name'], 'mb': round(m['size']/1048576, 1)})

async def chunk_status(request):
    m = _load(request.match_info['uid'])
    return web.json_response({k: v for k, v in m.items() if not k.startswith('_') and k != 'busy'})

async def uploads(request):
    return web.json_response({'active': [{'name': m['name'], 'received': m['received'], 'size': m['size']} for m in active_uploads()]})

# ---------- 人物碎片素材库 ----------
KEYS = {'view': '角度', 'framing': '景别', 'pitch': '俯仰', 'expr': '表情'}
def _frag_folders():
    out = []
    if not INPUTS.is_dir(): return out
    for d in sorted((p for p in INPUTS.iterdir() if p.is_dir() and not p.name.startswith('.')), key=lambda p: -p.stat().st_mtime):
        if not (d.name.endswith('_frames') or (d/'tags.json').is_file()): continue
        out.append(d)
    return out

async def frag_index(request):
    import tagger
    res = []
    for d in _frag_folders():
        imgs = [p.name for p in d.iterdir() if p.is_file() and p.suffix.lower() in tagger.IMG]
        tags = tagger.load(d) or {}
        cov = tagger.coverage({k: v for k, v in tags.items() if k in set(imgs)}) if tags else None
        res.append({'folder': d.name, 'video': d.name[:-7] if d.name.endswith('_frames') else d.name, 'count': len(imgs),
                    'tagged': len([k for k in imgs if k in tags]), 'counts': cov and cov['counts'], 'missing': cov and cov['missing'],
                    'few': cov and cov['few']})
    st = request.app.get('extract_state')
    live = {'folder': st['folder'], 'state': st['state'], 'phase': st.get('phase')} if st and st.get('state') == 'running' else None
    return web.json_response({'folders': res, 'running': live, 'keys': KEYS})

async def frag_files(request):
    import tagger
    name = video._fname(request.query.get('folder', '')); d = INPUTS/name
    key = request.query.get('key', ''); val = request.query.get('val', '')
    tags = tagger.load(d) or {}
    fs = sorted((p for p in d.iterdir() if p.is_file() and p.suffix.lower() in tagger.IMG), key=lambda p: p.name)
    if key in KEYS and val:
        fs = [p for p in fs if (tags.get(p.name) or {}).get(key, '未知') == val]
    elif key == 'untagged':
        fs = [p for p in fs if p.name not in tags]
    return web.json_response({'files': [{'name': name + '/' + p.name, 'tags': {k: (tags.get(p.name) or {}).get(k) for k in KEYS}} for p in fs[:5000]],
                              'total': len(fs)})

def setup(app):
    app.add_routes([web.post('/api/videos/chunk', init), web.put('/api/videos/chunk/{uid}', put_chunk),
                    web.get('/api/videos/chunk/{uid}', chunk_status), web.post('/api/videos/chunk/{uid}/finish', finish),
                    web.get('/api/videos/uploads', uploads),
                    web.get('/api/fragments', frag_index), web.get('/api/fragments/files', frag_files)])
