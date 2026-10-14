# 空闲自动关机辅助：记录最近用户请求时间 + 汇报后台任务是否在跑（只看状态，不碰内容）
import time, os
from aiohttp import web
STAMP = "/root/autodl-tmp/.studio_last_req"
QUIET = ("/api/idle_status", "/api/health", "/public_url.txt")
_last = [0.0]
@web.middleware
async def touch(request, handler):
    if not request.path.startswith(QUIET):
        now = time.time()
        if now - _last[0] > 5:
            _last[0] = now
            try: open(STAMP, "w").write(str(int(now)))
            except Exception: pass
    return await handler(request)
def _busy(app):
    n = 0
    for k, v in list(app.items()):
        vals = [v] if isinstance(v, dict) and "state" in v else (list(v.values()) if isinstance(v, dict) else [])
        for j in vals:
            if isinstance(j, dict) and j.get("state") in ("queued", "running", "cancelling"): n += 1
    # Only state metadata; no media enumeration or reads.
    from pathlib import Path
    import json
    for name in ('faceswap_queue','dreamid_queue'):
        for p in (Path(__file__).parent/name).glob('*.json'):
            try:
                if json.loads(p.read_text()).get('state') in ('staging','running','collecting'): n += 1
            except (OSError,ValueError): pass
    return n
async def idle_status(request):
    return web.json_response({"busy": _busy(request.app)})
async def public_url(request):
    try: return web.Response(text=open("/root/autodl-tmp/public_url.txt").read())
    except Exception: return web.Response(text="", status=404)
def setup(app):
    app.add_routes([web.get("/api/idle_status", idle_status), web.get("/public_url.txt", public_url)])
