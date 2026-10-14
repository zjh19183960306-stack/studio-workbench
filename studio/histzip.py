"""单张生成历史 → 一键打包下载（zip，ZIP_STORED，流式，不落盘）。只按文件处理。"""
import asyncio, time, zipfile
from urllib.parse import quote
from aiohttp import web
import library

class _W:
    """zipfile 的不可 seek 写入端：把数据块交给事件循环里的 resp.write。"""
    def __init__(self, loop, resp): self.loop, self.resp, self.n = loop, resp, 0
    def write(self, b):
        if b: asyncio.run_coroutine_threadsafe(self.resp.write(bytes(b)), self.loop).result(); self.n += len(b)
        return len(b)
    def tell(self): return self.n
    def flush(self): pass

def _pick(request):
    allf = {p.name: p for p in library._images(library.HIST)}
    want = request.query.getall('f', [])
    if not want: return sorted(allf.values(), key=lambda p: p.name)
    if len(want) > 5000: raise web.HTTPBadRequest(text='too many')
    out = []
    for n in dict.fromkeys(want):
        if n not in allf: raise web.HTTPNotFound(text='文件不存在：' + n[:80])
        out.append(allf[n])
    return out

def _zip(files, w):
    with zipfile.ZipFile(w, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as z:
        for p in files:
            zi = zipfile.ZipInfo.from_file(p, p.name); zi.compress_type = zipfile.ZIP_STORED
            with open(p, 'rb') as src, z.open(zi, 'w', force_zip64=zi.file_size > 0x7fffffff) as dst:
                while True:
                    b = src.read(1 << 20)
                    if not b: break
                    dst.write(b)

async def hist_zip(request):
    files = _pick(request)
    if not files: raise web.HTTPNotFound(text='还没有单张生成的图片')
    name = '工作台单张历史-%s.zip' % time.strftime('%Y%m%d-%H%M%S')
    resp = web.StreamResponse(headers={'Content-Type': 'application/zip', 'Cache-Control': 'no-store',
        'Content-Disposition': "attachment; filename=\"history.zip\"; filename*=UTF-8''%s" % quote(name)})
    await resp.prepare(request)
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _zip, files, _W(loop, resp))
    await resp.write_eof()
    return resp

async def hist_zipinfo(request):
    fs = library._images(library.HIST)
    return web.json_response({'count': len(fs), 'bytes': sum(p.stat().st_size for p in fs)})

def setup(app):
    app.add_routes([web.get('/api/history/zip', hist_zip), web.get('/api/history/zipinfo', hist_zipinfo)])
