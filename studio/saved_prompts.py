# 单张生成「常用提示词」：只做存取，不打印内容
from __future__ import annotations
import asyncio, json, os, tempfile, time
from pathlib import Path
from aiohttp import web
FILE = Path(os.environ.get('SAVED_PROMPTS_FILE') or (Path(__file__).resolve().parent / 'saved_prompts.json'))
MAX_NAME, MAX_TEXT, MAX_ITEMS = 40, 4000, 500
_lock = asyncio.Lock()

def _load():
    try: d = json.loads(FILE.read_text('utf-8'))
    except FileNotFoundError: return []
    items = d.get('items') if isinstance(d, dict) else None
    return [i for i in (items or []) if isinstance(i, dict) and isinstance(i.get('name'), str) and isinstance(i.get('text'), str)]

def _save(items):
    FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.saved_prompts.', suffix='.tmp', dir=str(FILE.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump({'items': items}, f, ensure_ascii=False); f.flush(); os.fsync(f.fileno())
        os.chmod(tmp, 0o600); os.replace(tmp, FILE)
    except BaseException:
        try: os.unlink(tmp)
        except OSError: pass
        raise

def _name(v):
    n = str(v or '').strip()
    if not n: raise ValueError('名字不能为空')
    if len(n) > MAX_NAME: raise ValueError(f'名字最多 {MAX_NAME} 个字')
    return n

def _auth(request):
    if not request.get('owner'): raise web.HTTPForbidden(text='请重新打开工作台页面')

async def _body(request):
    d = await request.json()
    if not isinstance(d, dict): raise ValueError('请求格式不对')
    return d

async def list_(request):
    _auth(request)
    async with _lock: items = _load()
    return web.json_response({'items': items, 'max_items': MAX_ITEMS, 'max_name': MAX_NAME, 'max_text': MAX_TEXT})

async def save(request):
    _auth(request); d = await _body(request)
    n = _name(d.get('name')); t = str(d.get('text') or '')
    if not t.strip(): raise ValueError('提示词内容不能为空')
    if len(t) > MAX_TEXT: raise ValueError(f'提示词最多 {MAX_TEXT} 个字')
    async with _lock:
        items = _load(); hit = next((i for i in items if i['name'] == n), None)
        if hit and not d.get('overwrite'): return web.json_response({'error': '已有同名提示词', 'code': 'exists'}, status=409)
        if hit: hit.update(text=t, updated=int(time.time()))
        else:
            if len(items) >= MAX_ITEMS: raise ValueError(f'最多保存 {MAX_ITEMS} 条，请先删除一些')
            items.append({'name': n, 'text': t, 'updated': int(time.time())})
        _save(items)
    return web.json_response({'ok': True, 'count': len(items)})

async def delete(request):
    _auth(request); n = _name((await _body(request)).get('name'))
    async with _lock:
        items = _load(); left = [i for i in items if i['name'] != n]
        if len(left) == len(items): raise ValueError('找不到这条提示词')
        _save(left)
    return web.json_response({'ok': True, 'count': len(left)})

async def rename(request):
    _auth(request); d = await _body(request); o = _name(d.get('old')); n = _name(d.get('new'))
    async with _lock:
        items = _load(); hit = next((i for i in items if i['name'] == o), None)
        if not hit: raise ValueError('找不到这条提示词')
        if o != n and any(i['name'] == n for i in items): raise ValueError('新名字已被占用')
        hit['name'] = n; hit['updated'] = int(time.time()); _save(items)
    return web.json_response({'ok': True})

def setup(app):
    app.add_routes([web.get('/api/saved-prompts', list_), web.post('/api/saved-prompts', save),
                    web.post('/api/saved-prompts/delete', delete), web.post('/api/saved-prompts/rename', rename)])
