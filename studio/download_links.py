"""Opaque, signed 24-hour links to explicitly selected output images."""
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit
from aiohttp import web

HERE = Path(__file__).resolve().parent
ROOT = Path('/root/autodl-tmp/outputs')
STORE = HERE / '.download_links'
PUBLIC = 'https://bbstudio6006.serveousercontent.com'
TTL = 86400

def key():
    STORE.mkdir(mode=0o700, exist_ok=True)
    p = STORE / 'key'
    try:
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, 'wb') as f:
            f.write(secrets.token_bytes(32))
    return p.read_bytes()

def signature(ident, expiry):
    return hmac.new(key(), f'v1:{ident}:{expiry}'.encode(), hashlib.sha256).hexdigest()

def checked(path):
    root = ROOT.resolve()
    p = path.resolve()
    if not p.is_relative_to(root) or p == root or p.suffix.lower() not in ('.png', '.jpg', '.jpeg', '.webp') or not p.is_file():
        raise web.HTTPForbidden(text='Invalid output')
    return p

def fingerprint(p):
    s = p.stat()
    return [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns]

def mint(p, number=1):
    p = checked(p)
    ident = secrets.token_hex(24)
    expiry = int(time.time()) + TTL
    key()
    record = {'path': str(p.relative_to(ROOT.resolve())), 'expiry': expiry,
              'name': f'output_{max(1, int(number))}{p.suffix.lower()}', 'stat': fingerprint(p)}
    fd = os.open(STORE / (ident + '.json'), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(record, f)
    return {'url': f'{PUBLIC}/dl/{ident}?expires={expiry}&sig={signature(ident, expiry)}',
            'expires_at': expiry, 'valid_seconds': TTL}

async def create(request):
    data = await request.json()
    url = urlsplit(data.get('url', ''))
    if url.scheme or url.netloc or url.query or url.fragment:
        raise web.HTTPBadRequest(text='Use an output endpoint')
    parts = [unquote(x) for x in url.path.split('/')]
    if any('/' in x or '\\' in x or x in ('.', '..') or '\x00' in x for x in parts[1:]):
        raise web.HTTPForbidden()
    number = 1
    if len(parts) == 5 and parts[1:4] == ['api', 'history', 'file']:
        p = ROOT / 'Qwen21' / parts[4]
    elif len(parts) == 6 and parts[1:3] == ['api', 'batches'] and parts[4] in ('files', 'rejected'):
        p = ROOT / parts[3]
        if parts[4] == 'rejected':
            p = p / '_rejected'
        p = p / parts[5]
    elif len(parts) == 6 and parts[1:3] == ['api', 'jobs'] and parts[4] == 'images':
        job = request.app['jobs'].get(parts[3])
        if not job or job.get('owner') != request['owner']:
            raise web.HTTPNotFound()
        index = int(parts[5])
        if index < 0 or index >= len(job['images']):
            raise web.HTTPNotFound()
        info = job['images'][index]
        if info.get('type', 'output') != 'output':
            raise web.HTTPForbidden()
        p = ROOT / info.get('subfolder', '') / info['filename']
        number = index + 1
    else:
        raise web.HTTPBadRequest(text='Only output images can be shared')
    return web.json_response(mint(p, number), headers={'Cache-Control': 'no-store'})

async def download(request):
    ident = request.match_info['ident']
    expiry = request.query.get('expires', '')
    sig = request.query.get('sig', '')
    if not re.fullmatch('[0-9a-f]{48}', ident) or not re.fullmatch('[0-9]{1,12}', expiry) or not re.fullmatch('[0-9a-f]{64}', sig):
        raise web.HTTPForbidden()
    if int(expiry) <= int(time.time()) or int(expiry) > int(time.time()) + TTL or not hmac.compare_digest(sig, signature(ident, expiry)):
        raise web.HTTPForbidden()
    try:
        record = json.loads((STORE / (ident + '.json')).read_text())
        if record['expiry'] != int(expiry):
            raise web.HTTPForbidden()
        p = checked(ROOT / record['path'])
        if fingerprint(p) != record['stat']:
            raise web.HTTPForbidden()
    except (OSError, ValueError, KeyError):
        raise web.HTTPForbidden()
    return web.FileResponse(p, headers={'Content-Disposition': f'attachment; filename="{record["name"]}"',
        'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer', 'Accept-Ranges': 'bytes'})

def setup(app):
    key()
    app.add_routes([web.post('/api/download-links', create), web.get('/dl/{ident}', download)])
