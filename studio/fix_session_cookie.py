from pathlib import Path
p = Path('/root/autodl-tmp/studio/server.py')
text = p.read_text(encoding='utf-8')
bak = Path('/root/autodl-tmp/studio/backups/auth_images_20261013/server.py.before_session_cookie')
bak.parent.mkdir(parents=True, exist_ok=True)
if not bak.exists():
    bak.write_text(text, encoding='utf-8')

old = """def set_auth_cookie(resp, request):
    resp.set_cookie('studio_auth', ACCESS_TOKEN, httponly=True, samesite='Lax',
                    max_age=30*86400, path='/', secure=_https(request))
    return resp"""

new = """def set_auth_cookie(resp, request):
    # Session cookie only: closing the browser requires password again.
    # Cookie name studio_auth_v2 invalidates any old 30-day studio_auth cookies.
    resp.set_cookie('studio_auth_v2', ACCESS_TOKEN, httponly=True, samesite='Lax',
                    path='/', secure=_https(request))
    # Clear legacy long-lived cookie if present.
    resp.del_cookie('studio_auth', path='/')
    return resp"""

if old not in text:
    raise SystemExit('set_auth_cookie block not found or already changed')
text = text.replace(old, new, 1)

# Update all studio_auth cookie reads/deletes for auth to v2, carefully.
# Keep del_cookie for both names on logout.
replacements = [
    ("request.cookies.get('studio_auth', '')", "request.cookies.get('studio_auth_v2', '')"),
]
for a, b in replacements:
    if a not in text:
        raise SystemExit('missing: ' + a)
    text = text.replace(a, b)

# logout should clear both
old_logout = "resp.del_cookie('studio_auth',path='/'); resp.del_cookie('studio_session',path='/')"
new_logout = "resp.del_cookie('studio_auth_v2',path='/'); resp.del_cookie('studio_auth',path='/'); resp.del_cookie('studio_session',path='/')"
if old_logout not in text:
    raise SystemExit('logout del_cookie not found')
text = text.replace(old_logout, new_logout, 1)

p.write_text(text, encoding='utf-8')
print('patched ok')
# sanity
assert "studio_auth_v2" in text
assert "max_age=30*86400" not in text or "studio_auth'," not in text.split('set_auth_cookie')[1][:400]
print('sanity ok')
