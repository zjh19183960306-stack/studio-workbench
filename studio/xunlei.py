"""单张生成历史 → 迅雷云盘备份（经本机 OpenList 127.0.0.1:5244）。
只传原图；按 文件名+大小 去重；传完列目录核对。密码不写日志、不回显。"""
import asyncio, html, json, os, re, subprocess, time
from pathlib import Path
from urllib.parse import quote
from aiohttp import ClientSession, ClientTimeout, web
import library

AL = 'http://127.0.0.1:5244'
ADIR = Path('/root/autodl-tmp/alist')
MOUNT = '/xunlei'
FOLDER = '工作台单张历史'
RECORD = ADIR/'xunlei_backup.json'
HIST = library.HIST
_tok = {'t': None}
STATE = {'state': 'idle', 'total': 0, 'done': 0, 'skipped': 0, 'failed': 0, 'verified': 0, 'msg': '', 'started': 0}
_lock = asyncio.Lock()

def ensure_alist():
    try: subprocess.run(['bash', str(ADIR/'start_alist.sh')], capture_output=True, timeout=30)
    except Exception: pass

def _load():
    try: return json.loads(RECORD.read_text())
    except Exception: return {}

def _save(d):
    tmp = RECORD.with_suffix('.tmp'); tmp.write_text(json.dumps(d, ensure_ascii=False)); os.replace(tmp, RECORD)

async def _req(s, method, path, retry=True, **kw):
    if not _tok['t']:
        pw = (ADIR/'admin_pass.txt').read_text().strip()
        async with s.post(AL+'/api/auth/login', json={'username': 'admin', 'password': pw}) as r:
            _tok['t'] = (await r.json())['data']['token']
    h = kw.pop('headers', {}); h['Authorization'] = _tok['t']
    async with s.request(method, AL+path, headers=h, **kw) as r:
        d = await r.json(content_type=None)
    if d.get('code') == 401 and retry:
        _tok['t'] = None; return await _req(s, method, path, False, headers=h, **kw)
    return d

def _sess(t=60): return ClientSession(timeout=ClientTimeout(total=None, sock_read=t, connect=10))

async def _storage(s):
    d = await _req(s, 'GET', '/api/admin/storage/list?page=1&per_page=200')
    for st in (d.get('data') or {}).get('content') or []:
        if st.get('mount_path') == MOUNT: return st
    return None

PENDING = ADIR/'xl_pending.json'
ANDROID_URL = 'https://i.xunlei.com/xlcaptcha/android.html'

def _plain(t):
    t = html.unescape(re.sub(r'<[^>]+>', ' ', str(t or '')))
    return re.sub(r'\s+', ' ', t).strip()[:200]

def _verify_info(text):
    """解析 Thunder 驱动的「本次登录需要验证」HTML / need verify 链接。返回 dict 或 None。"""
    t = str(text or '')
    m = re.search(r'<code>(.*?)</code>', t, re.S)
    if m and 'creditkey' in m.group(1):
        try:
            d = json.loads(html.unescape(m.group(1)))
            if d.get('reviewurl'): return {k: d.get(k) or '' for k in ('creditkey', 'reviewurl', 'deviceid', 'devicesign')}
        except Exception: pass
    m = re.search(r'need verify:.*?href="([^"]+)"', t, re.S)
    if m: return {'reviewurl': html.unescape(m.group(1)), 'creditkey': ''}
    return None

def _save_pending(v):
    fd = os.open(str(PENDING), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f: json.dump(v, f)

def _load_pending():
    try: return json.loads(PENDING.read_text())
    except Exception: return None

def _clean_status(st, extra=''):
    if not st: return {'configured': False, 'state': 'none'}
    raw = st.get('status') or ''
    out = {'configured': True, 'driver': st.get('driver'), 'disabled': st.get('disabled'),
           'username': (json.loads(st.get('addition') or '{}').get('username') or '')[:3] + '****'}
    v = _verify_info(raw) or _verify_info(extra)
    if v:
        if v.get('creditkey') or not _load_pending(): _save_pending(v)
        out.update(state='need_verify', status='need_verify', reviewurl=v['reviewurl'], android_url=ANDROID_URL)
        p = _load_pending() or v
        if p.get('creditkey'):
            out['verify_cmd'] = 'reviewCb(' + json.dumps({k: p.get(k, '') for k in ('creditkey', 'reviewurl', 'deviceid', 'devicesign')}, ensure_ascii=False) + ')'
    elif raw == 'work' and not st.get('disabled'):
        out.update(state='work', status='work')
    else:
        out.update(state='error', status='error', error=_plain(extra or raw) or '未知错误')
    return out

async def _account(s, extra=''):
    return _clean_status(await _storage(s), extra)

async def xl_status(request):
    try:
        async with _sess(15) as s: st = await _account(s)
    except Exception as e: st = {'configured': False, 'state': 'none', 'error': 'OpenList 没有在运行：' + type(e).__name__}
    return web.json_response({'account': st, 'upload': STATE})

def _body(old, add):
    body = {'mount_path': MOUNT, 'driver': 'Thunder', 'order': 0, 'remark': '工作台单张历史备份',
            'cache_expiration': 0, 'web_proxy': False, 'webdav_policy': 'native_proxy', 'down_proxy_url': '',
            'extract_folder': '', 'enable_sign': False, 'addition': json.dumps(add, ensure_ascii=False)}
    if old: body = {**old, **body, 'id': old['id'], 'disabled': False}
    return body

async def _apply(s, old, add):
    r = await _req(s, 'POST', '/api/admin/storage/update' if old else '/api/admin/storage/create', json=_body(old, add))
    acc = await _account(s, '' if r.get('code') == 200 else (r.get('message') or ''))
    if acc.get('state') == 'work':
        try: PENDING.unlink()
        except FileNotFoundError: pass
    return {'ok': acc.get('state') == 'work', 'account': acc}

async def xl_login(request):
    d = await request.json()
    user = str(d.get('username') or '').strip(); pw = str(d.get('password') or ''); ck = str(d.get('credit_key') or '').strip()
    if not user: raise web.HTTPBadRequest(text='请填写迅雷账号（手机号）')
    async with _sess(60) as s:
        old = await _storage(s)
        add = json.loads(old.get('addition') or '{}') if old else {}
        if not pw and not add.get('password'): raise web.HTTPBadRequest(text='请填写密码')
        add.update({'username': user, 'root_folder_id': add.get('root_folder_id', '')})
        if pw: add['password'] = pw
        if ck: add['credit_key'] = ck
        try: PENDING.unlink()
        except FileNotFoundError: pass
        return web.json_response(await _apply(s, old, add))

async def xl_continue(request):
    """峻弘在迅雷验证页验证完后：用存好的账号密码 + creditkey 重新加载存储。"""
    async with _sess(60) as s:
        old = await _storage(s)
        if not old: raise web.HTTPBadRequest(text='还没有填写迅雷账号')
        add = json.loads(old.get('addition') or '{}')
        try: body = await request.json()
        except Exception: body = {}
        ck = str((body or {}).get('creditkey') or '').strip().strip('"').strip()
        pend = _load_pending() or _verify_info(old.get('status') or '') or {}
        ck = ck or pend.get('creditkey') or ''
        if ck: add['credit_key'] = ck
        return web.json_response(await _apply(s, old, add))

async def _list_remote(s):
    out = {}; page = 1
    while True:
        d = await _req(s, 'POST', '/api/fs/list', json={'path': f'{MOUNT}/{FOLDER}', 'refresh': page == 1, 'page': page, 'per_page': 500})
        if d.get('code') != 200: raise RuntimeError(d.get('message') or '列目录失败')
        c = (d.get('data') or {}).get('content') or []
        for f in c:
            if not f.get('is_dir'): out[f['name']] = f.get('size')
        if len(c) < 500: return out
        page += 1

def backed_now():
    rec = _load(); res = []
    for p in library._images(HIST):
        if rec.get(p.name) == p.stat().st_size: res.append(p.name)
    return res

async def _run():
    STATE.update(state='running', total=0, done=0, skipped=0, failed=0, verified=0, msg='', started=time.time())
    try:
        files = sorted(library._images(HIST), key=lambda p: p.stat().st_mtime)
        rec = _load(); STATE['total'] = len(files)
        async with _sess(300) as s:
            await _req(s, 'POST', '/api/fs/mkdir', json={'path': f'{MOUNT}/{FOLDER}'})
            todo = []
            for p in files:
                if rec.get(p.name) == p.stat().st_size: STATE['skipped'] += 1
                else: todo.append(p)
            for p in todo:
                size = p.stat().st_size
                try:
                    with open(p, 'rb') as fh:
                        d = await _req(s, 'PUT', '/api/fs/put', data=fh, headers={
                            'File-Path': quote(f'{MOUNT}/{FOLDER}/{p.name}'), 'Content-Length': str(size),
                            'Content-Type': 'application/octet-stream', 'As-Task': 'false', 'Overwrite': 'true'})
                    if d.get('code') == 200: STATE['done'] += 1
                    else: STATE['failed'] += 1; STATE['msg'] = (d.get('message') or '')[:300]
                except Exception as e:
                    STATE['failed'] += 1; STATE['msg'] = type(e).__name__
            STATE['msg'] = STATE['msg'] or '正在核对迅雷上的文件…'
            remote = await _list_remote(s)
        n = 0
        for p in files:
            sz = p.stat().st_size
            if remote.get(p.name) == sz: rec[p.name] = sz; n += 1
            elif p.name in rec and remote.get(p.name) != sz: rec.pop(p.name, None)
        _save(rec); STATE['verified'] = n
        miss = len(files) - n
        STATE['msg'] = '核对完成：迅雷上有 %d 张与本地一致' % n + ('，%d 张未对上' % miss if miss else '')
        STATE['state'] = 'done'
    except Exception as e:
        STATE['state'] = 'error'; STATE['msg'] = (str(e) or type(e).__name__)[:300]

async def xl_upload(request):
    if STATE['state'] == 'running': return web.json_response({'ok': True, 'already': True, 'upload': STATE})
    try:
        async with _sess(15) as s: acc = await _account(s)
    except Exception: acc = {}
    if acc.get('state') != 'work':
        return web.json_response({'ok': False, 'message': '请先登录迅雷', 'upload': STATE}, status=409)
    STATE['state'] = 'running'; STATE['msg'] = ''
    request.app['xl_task'] = asyncio.get_running_loop().create_task(_run())
    return web.json_response({'ok': True, 'upload': STATE})

async def xl_backed(request):
    return web.json_response({'files': backed_now()})

async def _startup(app):
    await asyncio.get_running_loop().run_in_executor(None, ensure_alist)

def setup(app):
    # app.on_startup.append(_startup)  # 已停用：不再自动拉起 OpenList
    app.add_routes([web.get('/api/xunlei/status', xl_status), web.post('/api/xunlei/login', xl_login), web.post('/api/xunlei/continue', xl_continue),
                    web.post('/api/xunlei/upload', xl_upload), web.get('/api/xunlei/backed', xl_backed)])
