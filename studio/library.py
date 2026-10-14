"""素材库（浏览/建文件夹/改名/移动）+ 单张生成历史。仅限 inputs、videos、outputs/Qwen21。"""
import json, os, re, subprocess
from pathlib import Path
from aiohttp import web
import thumbs

R = Path('/root/autodl-tmp'); INPUTS = R/'inputs'; VIDEOS = R/'videos'; HIST = R/'outputs'/'Qwen21'
IN = INPUTS  # alias for older callers / lbcache
IMG = re.compile(r'[^/\\\x00]+\.(png|jpe?g|webp)\Z', re.I)
FOLDER = re.compile(r'[^/\\\x00.][^/\\\x00]{0,99}\Z')

def _folder(name):
    if name in ('', None): return INPUTS
    if not isinstance(name, str) or not FOLDER.fullmatch(name) or name.strip() != name: raise ValueError('文件夹名无效')
    return INPUTS/name

def _busy(app):
    s = app.get('pack_state'); return bool(s and s['state'] == 'running')

def _images(d):
    return [p for p in d.iterdir() if p.is_file() and IMG.fullmatch(p.name) and not p.name.startswith(('.', '_blank_canvas'))] if d.is_dir() else []

_dur = {}
def _duration(p):
    k = (str(p), p.stat().st_mtime_ns)
    if k not in _dur:
        try: _dur[k] = round(float(subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', str(p)],
                                                   capture_output=True, text=True, timeout=20).stdout.strip() or 0))
        except Exception: _dur[k] = None
    return _dur[k]

async def lib_index(request):
    INPUTS.mkdir(parents=True, exist_ok=True)
    folders = [{'name': '', 'label': 'inputs（根目录）', 'count': len(_images(INPUTS))}]
    for d in sorted(p for p in INPUTS.iterdir() if p.is_dir() and FOLDER.fullmatch(p.name)):
        folders.append({'name': d.name, 'label': d.name, 'count': len(_images(d))})
    vids = []
    if VIDEOS.is_dir():
        for p in sorted(VIDEOS.iterdir(), key=lambda p: -p.stat().st_mtime):
            if p.is_file() and not p.name.startswith('.'):
                vids.append({'name': p.name, 'mb': round(p.stat().st_size/1048576, 1), 'duration_s': _duration(p)})
    return web.json_response({'folders': folders, 'videos': vids})

async def lib_files(request):
    d = _folder(request.query.get('folder', '')); q = request.query.get('q', '').strip().lower()
    fs = sorted(_images(d), key=lambda p: -p.stat().st_mtime)
    if q: fs = [p for p in fs if q in p.name.lower()]
    pre = (d.name + '/') if d != INPUTS else ''
    return web.json_response({'files': [pre + p.name for p in fs[:3000]], 'total': len(fs)})

async def lib_mkdir(request):
    d = _folder((await request.json()).get('name'))
    if d == INPUTS: raise ValueError('请填写文件夹名')
    if d.exists(): raise ValueError('已有同名文件夹')
    d.mkdir(); return web.json_response({'ok': True})

async def lib_rename(request):
    data = await request.json(); a = _folder(data.get('old')); b = _folder(data.get('new'))
    if a == INPUTS or b == INPUTS: raise ValueError('根目录不能改名')
    if _busy(request.app): raise ValueError('素材包正在生成，请稍后再改名')
    if not a.is_dir(): raise ValueError('找不到文件夹')
    if b.exists(): raise ValueError('已有同名文件夹')
    a.rename(b)
    import shutil; shutil.rmtree(thumbs.THUMBS/'inputs', ignore_errors=True)  # 缩略图按需重建
    return web.json_response({'ok': True})

def _unique(d, name):
    stem, ext = name.rsplit('.', 1)
    for n in range(10000):
        cand = d/(name if n == 0 else f'{stem}_{n}.{ext}')
        if not cand.exists(): return cand
    raise ValueError('同名文件过多')

async def lib_move(request):
    data = await request.json(); files = data.get('files'); to = _folder(data.get('to', ''))
    if _busy(request.app): raise ValueError('素材包正在生成，请稍后再移动')
    if not to.is_dir(): raise ValueError('目标文件夹不存在')
    if not isinstance(files, list) or not 1 <= len(files) <= 3000: raise ValueError('请选择文件')
    moved = 0
    for n in files:
        parts = n.split('/') if isinstance(n, str) else []
        if len(parts) == 1: src = INPUTS/parts[0]
        elif len(parts) == 2: src = _folder(parts[0])/parts[1]
        else: raise ValueError('文件名无效')
        if not IMG.fullmatch(src.name) or not src.resolve().is_relative_to(INPUTS.resolve()) or not src.is_file(): raise ValueError('文件无效：' + str(n)[:80])
        if src.parent == to: continue
        os.rename(src, _unique(to, src.name)); thumbs.drop_thumb('inputs', *parts); moved += 1
        if src.parent != INPUTS and not any(src.parent.iterdir()) and src.parent.name.endswith('_frames'): pass
    return web.json_response({'moved': moved})

# ---- 单张生成历史 ----
def _hist_name(n):
    if not isinstance(n, str) or not IMG.fullmatch(n) or n.startswith('.') or not (HIST/n).is_file(): raise web.HTTPNotFound()
    return n

async def hist_list(request):
    page = max(1, int(request.query.get('page', '1') or 1)); per = min(120, max(6, int(request.query.get('per', '48') or 48)))
    fs = sorted(_images(HIST), key=lambda p: -p.stat().st_mtime)
    return web.json_response({'files': [p.name for p in fs[(page-1)*per: page*per]], 'total': len(fs), 'page': page, 'per': per})

async def hist_file(request):
    n = _hist_name(request.match_info['name'])
    if request.query.get('thumb') == '1': return await thumbs.thumb_response(request, HIST/n, 'history', n)
    return await thumbs.file_response(request, HIST/n, download=request.query.get('download') == '1', max_age=3600)

async def hist_delete_many(request):
    d = await request.json(); fs = d.get('files') if isinstance(d, dict) else None
    if not isinstance(fs, list) or len(fs) > 1000: raise web.HTTPBadRequest(text='bad files')
    names = [_hist_name(n) for n in fs]; n0 = 0
    for n in names:
        (HIST/n).unlink(missing_ok=True); thumbs.drop_thumb('history', n); n0 += 1
    return web.json_response({'ok': True, 'deleted': n0})

async def hist_delete(request):
    n = _hist_name(request.match_info['name']); (HIST/n).unlink(); thumbs.drop_thumb('history', n)
    return web.json_response({'ok': True})

def setup(app):
    app.add_routes([web.get('/api/lib', lib_index), web.get('/api/lib/files', lib_files), web.post('/api/lib/mkdir', lib_mkdir),
                    web.post('/api/lib/rename', lib_rename), web.post('/api/lib/move', lib_move),
                    web.get('/api/history', hist_list), web.get('/api/history/file/{name}', hist_file),
                    web.delete('/api/history/file/{name}', hist_delete), web.post('/api/history/delete', hist_delete_many)])
