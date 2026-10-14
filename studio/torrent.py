"""单张生成历史 → 种子/磁力链接（BEP19 web seed，云机无入站端口也能用迅雷下）。只按文件处理，不读图片内容以外的东西。"""
import asyncio, hashlib, os, re, shutil, time
from pathlib import Path
from urllib.parse import quote
from aiohttp import web
import library

SNAP = library.R/'outputs'/'_torrent'
NAME = '工作台单张历史'
PIECE = 1 << 20
KEEP = 2
TS = re.compile(r'\d{8}-\d{6}\Z')

def bencode(x):
    if isinstance(x, int): return b'i%de' % x
    if isinstance(x, str): x = x.encode()
    if isinstance(x, bytes): return b'%d:' % len(x) + x
    if isinstance(x, list): return b'l' + b''.join(bencode(i) for i in x) + b'e'
    if isinstance(x, dict):
        return b'd' + b''.join(bencode(k) + bencode(x[k]) for k in sorted(x, key=lambda k: k.encode() if isinstance(k, str) else k)) + b'e'
    raise TypeError(type(x))

def bdecode(b, i=0):
    c = b[i:i+1]
    if c == b'i': j = b.index(b'e', i); return int(b[i+1:j]), j+1
    if c == b'l':
        i += 1; out = []
        while b[i:i+1] != b'e': v, i = bdecode(b, i); out.append(v)
        return out, i+1
    if c == b'd':
        i += 1; out = {}
        while b[i:i+1] != b'e': k, i = bdecode(b, i); v, i = bdecode(b, i); out[k] = v
        return out, i+1
    j = b.index(b':', i); n = int(b[i:j]); return b[j+1:j+1+n], j+1+n

def build(src_files, root):
    """快照到 root/NAME/，生成 info 字典。"""
    content = root/NAME; content.mkdir(parents=True)
    files = []
    for p in sorted(src_files, key=lambda p: p.name):
        dst = content/p.name
        try: os.link(p, dst)
        except OSError: shutil.copy2(p, dst)
        files.append(dst)
    h = []; buf = bytearray()
    for f in files:
        with open(f, 'rb') as fh:
            while True:
                chunk = fh.read(PIECE - len(buf))
                if not chunk: break
                buf += chunk
                if len(buf) == PIECE: h.append(hashlib.sha1(buf).digest()); buf = bytearray()
    if buf: h.append(hashlib.sha1(buf).digest())
    return {'name': NAME, 'piece length': PIECE, 'pieces': b''.join(h),
            'files': [{'length': f.stat().st_size, 'path': [f.name]} for f in files]}

def prune(keep=KEEP):
    if not SNAP.is_dir(): return
    ds = sorted([d for d in SNAP.iterdir() if d.is_dir() and TS.fullmatch(d.name)], key=lambda d: d.name)
    for d in ds[:-keep] if keep else ds: shutil.rmtree(d, ignore_errors=True)

def _make(host):
    files = library._images(library.HIST)
    if not files: raise web.HTTPBadRequest(text='还没有单张生成的图片')
    ts = time.strftime('%Y%m%d-%H%M%S')
    while (SNAP/ts).exists(): time.sleep(1); ts = time.strftime('%Y%m%d-%H%M%S')
    root = SNAP/ts; root.mkdir(parents=True)
    try:
        info = build(files, root)
        urls = ['http://127.0.0.1:6006/api/torrent-seed/%s/' % ts]
        if host and host not in ('127.0.0.1:6006',) and re.fullmatch(r'[A-Za-z0-9.\-:\[\]]+', host):
            urls.append('http://%s/api/torrent-seed/%s/' % (host, ts))
        tor = {'info': info, 'url-list': urls, 'created by': 'studio', 'creation date': int(time.time())}
        (root/'meta.torrent').write_bytes(bencode(tor))
    except Exception:
        shutil.rmtree(root, ignore_errors=True); raise
    prune()
    ih = hashlib.sha1(bencode(info)).hexdigest()
    total = sum(f['length'] for f in info['files'])
    mag = 'magnet:?xt=urn:btih:%s&dn=%s&xl=%d' % (ih, quote(NAME), total) + ''.join('&ws=' + quote(u, safe='') for u in urls)
    return {'ts': ts, 'infohash': ih, 'magnet': mag, 'torrent_url': '/api/history/torrent/%s.torrent' % ts,
            'files': len(info['files']), 'total': total, 'web_seeds': urls}

_lock = asyncio.Lock()
async def make_torrent(request):
    async with _lock:
        return web.json_response(await asyncio.get_running_loop().run_in_executor(None, _make, request.host))

async def get_torrent(request):
    ts = request.match_info['ts']
    if not TS.fullmatch(ts): raise web.HTTPNotFound()
    p = SNAP/ts/'meta.torrent'
    if not p.is_file(): raise web.HTTPNotFound()
    return web.Response(body=p.read_bytes(), content_type='application/x-bittorrent',
                        headers={'Content-Disposition': "attachment; filename*=UTF-8''%s" % quote('%s-%s.torrent' % (NAME, ts))})

async def seed(request):
    ts = request.match_info['ts']; rel = request.match_info['path']
    if not TS.fullmatch(ts) or '\x00' in rel or '\\' in rel: raise web.HTTPNotFound()
    base = (SNAP/ts/NAME).resolve()
    parts = rel.split('/')
    if len(parts) != 2 or parts[0] != NAME or parts[1] in ('', '.', '..'): raise web.HTTPNotFound()
    p = (SNAP/ts/NAME/parts[1]).resolve()
    if p.parent != base or not p.is_file(): raise web.HTTPNotFound()
    return web.FileResponse(p, headers={'Cache-Control': 'no-store', 'Accept-Ranges': 'bytes'})

def setup(app):
    app.add_routes([web.post('/api/history/torrent', make_torrent),
                    web.get(r'/api/history/torrent/{ts}.torrent', get_torrent),
                    web.get('/api/torrent-seed/{ts}/{path:.+}', seed)])
