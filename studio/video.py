"""视频上传 + 1fps 抽帧 + 本地自动筛选（模糊/重复/无人）。全部本机处理，不联网，不回传画面。"""
import asyncio, json, os, re, shutil, subprocess, time, hashlib, tempfile
from urllib.parse import quote
from pathlib import Path
from aiohttp import web

R = Path('/root/autodl-tmp'); VIDEOS = R/'videos'; INPUTS = R/'inputs'
VID = re.compile(r'[^/\\\x00]+\.(mp4|mov|mkv|webm|m4v)\Z', re.I)
MAX_VIDEO = 20 * 1024**3

def _vname(name):
    if not isinstance(name, str) or not VID.fullmatch(name) or name.startswith('.'): raise ValueError('视频名称无效')
    if not (VIDEOS/name).is_file(): raise ValueError('找不到视频：' + name)
    return name


# Small, lazy video covers; filenames never appear in cache paths or error logs.
THUMBS = Path(__file__).parent/'video_thumbs'
FFMPEG = shutil.which('ffmpeg') or '/usr/bin/ffmpeg'
FFPROBE = shutil.which('ffprobe') or '/usr/bin/ffprobe'
_cover_slots = asyncio.Semaphore(2)
_cover_locks = {}
_cover_failures = {}

def _cover_key(name):
    return hashlib.sha256(name.encode('utf-8')).hexdigest()

def _cover_path(src):
    st = src.stat()
    return THUMBS/(_cover_key(src.name)+f'-{st.st_mtime_ns}-{st.st_size}.jpg')

def delete_cover(name):
    for p in THUMBS.glob(_cover_key(name)+'-*'):
        p.unlink(missing_ok=True)

def _make_cover(src, dest):
    THUMBS.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='cover-', suffix='.jpg', dir=THUMBS)
    os.close(fd)
    try:
        for seek in ('0.5', '0'):
            result = subprocess.run([FFMPEG, '-nostdin', '-hide_banner', '-loglevel', 'error',
                '-threads', '1', '-ss', seek, '-i', str(src), '-map', '0:v:0',
                '-frames:v', '1', '-vf', "scale=320:180:force_original_aspect_ratio=decrease",
                '-filter_threads', '1', '-q:v', '5', '-an', '-sn', '-y', tmp],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=12)
            if result.returncode == 0 and Path(tmp).stat().st_size > 0:
                # Source may have been deleted/replaced while ffmpeg ran.
                if not src.is_file() or _cover_path(src) != dest:
                    raise RuntimeError('source changed')
                delete_cover(src.name)
                os.replace(tmp, dest)
                # Hard disk budget: at most 32 MiB of regenerable covers.
                cached = sorted(THUMBS.glob('*.jpg'), key=lambda p:p.stat().st_mtime)
                size = sum(p.stat().st_size for p in cached)
                for old in cached:
                    if size <= 32*1024*1024: break
                    if old != dest:
                        size -= old.stat().st_size; old.unlink(missing_ok=True)
                return
        raise RuntimeError('no frame')
    finally:
        Path(tmp).unlink(missing_ok=True)

async def video_cover(request):
    name = request.match_info['name']
    try:
        _vname(name)
        src = VIDEOS/name
        if src.is_symlink(): raise ValueError('invalid source')
        dest = _cover_path(src)
    except (ValueError, OSError):
        return web.json_response({'error':'视频不存在或不可用'}, status=404)
    if not dest.is_file():
        if _cover_failures.get(str(dest), 0) > time.monotonic():
            return web.json_response({'error':'封面暂不可用，请稍后重试'}, status=422)
        key = _cover_key(name)
        lock, users = _cover_locks.setdefault(key, [asyncio.Lock(), 0])
        _cover_locks[key][1] += 1
        try:
            async with lock:
                if not dest.is_file():
                    if _cover_failures.get(str(dest), 0) > time.monotonic():
                        raise RuntimeError('retry later')
                    async with _cover_slots:
                        await asyncio.to_thread(_make_cover, src, dest)
        except Exception:
            now = time.monotonic()
            for k in list(_cover_failures):
                if _cover_failures[k] <= now: _cover_failures.pop(k, None)
            if len(_cover_failures) < 2048: _cover_failures[str(dest)] = now+60
            return web.json_response({'error':'无法生成封面，视频仍可正常选择'}, status=422)
        finally:
            _cover_locks[key][1] -= 1
            if not _cover_locks[key][1]: _cover_locks.pop(key, None)
    return web.FileResponse(dest, headers={'Cache-Control':'private, max-age=300', 'Content-Type':'image/jpeg'})


_duration_cache = {}

def _video_duration(src):
    key = (str(src), src.stat().st_mtime_ns, src.stat().st_size)
    if key in _duration_cache:
        return _duration_cache[key]
    try:
        out = subprocess.run([
            FFPROBE, '-v', 'error', '-show_entries', 'format=duration',
            '-of', 'csv=p=0', str(src)
        ], capture_output=True, text=True, timeout=8).stdout.strip()
        dur = float(out or 0)
    except Exception:
        dur = 0
    if len(_duration_cache) > 4096:
        _duration_cache.clear()
    _duration_cache[key] = dur
    return dur

def _fmt_duration(sec):
    sec = int(sec or 0)
    if sec <= 0:
        return ''
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    return f'{h}:{m:02d}:{s:02d}' if h else f'{m}:{s:02d}'

async def retry_cover(request):
    name = request.match_info['name']
    try:
        _vname(name)
        src = VIDEOS / name
        if src.is_symlink():
            raise ValueError('invalid source')
        dest = _cover_path(src)
    except (ValueError, OSError):
        return web.json_response({'error': '视频不存在或不可用'}, status=404)
    _cover_failures.pop(str(dest), None)
    delete_cover(name)
    dest = _cover_path(src)
    try:
        async with _cover_slots:
            await asyncio.to_thread(_make_cover, src, dest)
    except Exception:
        now = time.monotonic()
        _cover_failures[str(dest)] = now + 30
        return web.json_response({'error': '封面仍不可用，请稍后再试'}, status=422)
    return web.json_response({'ok': True, 'cover': '/api/videos/' + quote(name, safe='') + '/cover?v=' + str(src.stat().st_mtime_ns) + '&r=' + str(int(time.time()))})


_MIME = {'.mp4':'video/mp4','.mov':'video/quicktime','.mkv':'video/x-matroska','.webm':'video/webm','.m4v':'video/x-m4v'}

async def stream_video(request):
    """Authenticated byte-range video for in-browser play. Login required via guard."""
    try:
        name = _vname(request.match_info['name'])
    except ValueError:
        raise web.HTTPNotFound()
    src = VIDEOS / name
    ctype = _MIME.get(src.suffix.lower(), 'application/octet-stream')
    return web.FileResponse(src, headers={
        'Content-Type': ctype,
        'Accept-Ranges': 'bytes',
        'Cache-Control': 'private, max-age=0',
        'X-Content-Type-Options': 'nosniff',
    })

async def list_videos(request):
    VIDEOS.mkdir(parents=True, exist_ok=True)
    fs = sorted((p for p in VIDEOS.iterdir() if p.is_file() and VID.fullmatch(p.name)), key=lambda p: -p.stat().st_mtime)
    items = []
    for p in fs:
        try:
            dur = _video_duration(p)
        except Exception:
            dur = 0
        playable = bool(dur and dur > 0)
        items.append({
            'name': p.name,
            'mb': round(p.stat().st_size/1048576, 1),
            'duration': round(dur, 1) if dur else 0,
            'duration_text': _fmt_duration(dur),
            'playable': playable,
            'cover': '/api/videos/'+quote(p.name, safe='')+'/cover?v='+str(p.stat().st_mtime_ns),
            'url': '/api/videos/'+quote(p.name, safe='')+'/file'
        })
    return web.json_response({'videos': items})

async def upload_video(request):
    """流式上传：浏览器直接把文件作为请求体发来（不走 multipart，不进内存），同名自动加后缀。"""
    raw = Path(request.query.get('name', '')).name
    raw = re.sub(r'[\x00-\x1f/\\]', '_', raw).lstrip('.')
    if not VID.fullmatch(raw): raise ValueError('只支持 mp4/mov/mkv/webm/m4v')
    VIDEOS.mkdir(parents=True, exist_ok=True)
    stem, ext = raw.rsplit('.', 1); stem = stem[:120]
    for n in range(10000):
        name = f'{stem}.{ext}' if n == 0 else f'{stem}_{n}.{ext}'
        try: fd = os.open(VIDEOS/name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644); break
        except FileExistsError: continue
    else: raise ValueError('同名文件过多')
    size = 0; ok = False
    try:
        with os.fdopen(fd, 'wb') as f:
            async for chunk in request.content.iter_chunked(1 << 20):
                size += len(chunk)
                if size > MAX_VIDEO: raise ValueError('视频超过 20 GB')
                f.write(chunk)
        if size == 0: raise ValueError('视频为空')
        ok = True
    finally:
        if not ok: (VIDEOS/name).unlink(missing_ok=True)
    return web.json_response({'name': name, 'mb': round(size/1048576, 1)})

def _dest(video):
    return INPUTS / (Path(video).stem[:100] + '_frames')

def _extract_sync(state, video, use_person):
    import cv2, imagehash
    from PIL import Image
    src = VIDEOS/video; out = _dest(video); tmp = R/'.frames_tmp'/out.name
    shutil.rmtree(tmp, ignore_errors=True); tmp.mkdir(parents=True)
    out.mkdir(parents=True, exist_ok=True)
    # 时长用于进度
    try:
        dur = float(subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', str(src)],
                                   capture_output=True, text=True, timeout=60).stdout.strip() or 0)
    except Exception: dur = 0
    state.update(phase='抽帧中', total_est=int(dur) or None)
    p = subprocess.Popen(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(src), '-vf', 'fps=1', '-q:v', '2', str(tmp/'f_%06d.jpg')])
    while p.poll() is None:
        if state['stop']: p.kill(); break
        state['extracted'] = len(list(tmp.glob('f_*.jpg'))); time.sleep(1)
    frames = sorted(tmp.glob('f_*.jpg')); state['extracted'] = len(frames)
    if p.returncode not in (0, None) and not frames: raise RuntimeError('ffmpeg 抽帧失败（视频格式可能不支持）')
    hog = cv2.HOGDescriptor(); hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
    casc = [cv2.CascadeClassifier(cv2.data.haarcascades + n) for n in
            ('haarcascade_frontalface_default.xml', 'haarcascade_profileface.xml', 'haarcascade_upperbody.xml')]
    def has_person(gray, bgr):
        for c in casc:
            if len(c.detectMultiScale(gray, 1.1, 5, minSize=(40, 40))): return True
        rects, _ = hog.detectMultiScale(bgr, winStride=(8, 8), scale=1.05)
        return len(rects) > 0
    state.update(phase='筛选中', checked=0)
    blurs, hashes = [], []
    kept = blur = dup = noperson = 0
    for f in frames:
        if state['stop']: break
        img = cv2.imread(str(f))
        if img is None: state['checked'] += 1; continue
        h, w = img.shape[:2]; s = 720 / max(h, w)
        small = cv2.resize(img, (int(w*s), int(h*s))) if s < 1 else img
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        var = cv2.Laplacian(gray, cv2.CV_64F).var()
        state['checked'] += 1
        if var < 60: blur += 1; continue
        ph = imagehash.phash(Image.fromarray(cv2.cvtColor(small, cv2.COLOR_BGR2RGB)))
        if any(ph - o <= 6 for o in hashes[-30:]): dup += 1; continue
        if use_person and not has_person(gray, small): noperson += 1; continue
        hashes.append(ph)
        base = Path(video).stem[:60]
        target = out / f'{base}_{f.stem[2:]}.jpg'
        shutil.move(str(f), target); kept += 1
        state.update(kept=kept, blur=blur, dup=dup, noperson=noperson)
    state.update(kept=kept, blur=blur, dup=dup, noperson=noperson)
    shutil.rmtree(tmp, ignore_errors=True)
    if not state['stop']:
        try:
            import tagger; tagger.tag_folder(out, state)
        except Exception as e: state['tag_error'] = f'{type(e).__name__}:{str(e)[:200]}'
    return out.name

async def start_extract(request):
    app = request.app; data = await request.json()
    cur = app.get('extract_state')
    if cur and cur['state'] == 'running': raise ValueError('已有抽帧任务在进行')
    video = _vname(data.get('video'))
    use_person = data.get('person', True) is not False
    st = {'state': 'running', 'video': video, 'phase': '准备', 'extracted': 0, 'checked': 0, 'kept': 0, 'blur': 0, 'dup': 0,
          'noperson': 0, 'person_filter': use_person, 'folder': _dest(video).name, 'error': None, 'stop': False, 'started': time.time()}
    app['extract_state'] = st
    async def run():
        try:
            await asyncio.to_thread(_extract_sync, st, video, use_person)
            st['state'] = 'stopped' if st['stop'] else 'done'
        except Exception as e:
            st.update(state='error', error=str(e)[:300])
    t = asyncio.create_task(run()); app['tasks'].add(t); t.add_done_callback(app['tasks'].discard)
    return web.json_response({'ok': True, 'folder': st['folder']})

async def extract_status(request):
    s = request.app.get('extract_state')
    if not s: return web.json_response({'state': 'idle'})
    return web.json_response({k: v for k, v in s.items() if k != 'stop'} | {'elapsed_s': round(time.time() - s['started'])})

async def stop_extract(request):
    s = request.app.get('extract_state')
    if s and s['state'] == 'running': s['stop'] = True
    return web.json_response({'ok': True})

def _fname(name):
    if not isinstance(name, str) or not name or '/' in name or '\\' in name or name.startswith('.') or not (INPUTS/name).is_dir():
        raise ValueError('图片文件夹无效')
    return name

async def coverage(request):
    import tagger
    name = _fname(request.query.get('folder', ''))
    tags = tagger.load(INPUTS/name)
    if tags is None: return web.json_response({'folder': name, 'tagged': False})
    return web.json_response({'folder': name, 'tagged': True} | tagger.coverage(tags) | {'balanced': len(tagger.balanced(tags, 80))})

async def retag(request):
    app = request.app; d = await request.json(); name = _fname(d.get('folder'))
    cur = app.get('extract_state')
    if cur and cur['state'] == 'running': raise ValueError('已有抽帧/分类任务在进行')
    st = {'state': 'running', 'video': None, 'phase': '智能分类中', 'extracted': 0, 'checked': 0, 'kept': 0, 'blur': 0, 'dup': 0,
          'noperson': 0, 'folder': name, 'error': None, 'stop': False, 'started': time.time()}
    app['extract_state'] = st
    async def run():
        try:
            import tagger; await asyncio.to_thread(tagger.tag_folder, INPUTS/name, st)
            st['state'] = 'stopped' if st['stop'] else 'done'
        except Exception as e: st.update(state='error', error=str(e)[:300])
    t = asyncio.create_task(run()); app['tasks'].add(t); t.add_done_callback(app['tasks'].discard)
    return web.json_response({'ok': True})

def setup(app):
    app.add_routes([web.get('/api/videos', list_videos), web.post('/api/videos/upload', upload_video), web.get('/api/videos/{name}/cover', video_cover), web.post('/api/videos/{name}/cover/retry', retry_cover),
                    web.get('/api/videos/{name}/file', stream_video),
                    web.post('/api/extract', start_extract), web.get('/api/extract', extract_status),
                    web.post('/api/extract/stop', stop_extract),
                    web.get('/api/frames/coverage', coverage), web.post('/api/frames/retag', retag)])
