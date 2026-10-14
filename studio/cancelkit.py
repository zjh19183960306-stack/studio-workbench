"""Shared cancel helpers: drop Comfy prompts tagged with a job id, interrupt if running, free VRAM."""
import json, os, signal, urllib.request
COMFY = 'http://127.0.0.1:8192'

def _req(method, path, body=None, timeout=15):
    data = json.dumps(body).encode() if body is not None else None
    rq = urllib.request.Request(COMFY+path, data=data, method=method, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(rq, timeout=timeout) as r:
        t = r.read()
    try: return json.loads(t or b'{}')
    except ValueError: return {}

def comfy_cancel(tag):
    """tag = job id string that appears in the prompt graph (filename_prefix / input folder)."""
    out = {'deleted': 0, 'interrupted': False, 'freed': False}
    try:
        q = _req('GET', '/queue')
        pend = [it[1] for it in q.get('queue_pending', []) if tag in json.dumps(it[2] if len(it) > 2 else it)]
        run = [it[1] for it in q.get('queue_running', []) if tag in json.dumps(it[2] if len(it) > 2 else it)]
        if pend: _req('POST', '/queue', {'delete': pend}); out['deleted'] = len(pend)
        if run: _req('POST', '/interrupt', {}); out['interrupted'] = True
    except Exception as e:
        out['error'] = str(e)[:120]
    try: _req('POST', '/free', {'unload_models': True, 'free_memory': True}, timeout=60); out['freed'] = True
    except Exception: pass
    return out

def kill_depth_runner(jid):
    """Stop a running motion-extract depth runner for this job (own session -> killpg)."""
    p = '/root/autodl-tmp/motion_previews/%s/status.json' % jid
    try:
        d = json.load(open(p))
    except Exception:
        return False
    pid = d.get('pid'); killed = False
    if d.get('state') == 'running' and isinstance(pid, int) and pid > 1:
        for sig in (signal.SIGTERM,):
            try: os.killpg(os.getpgid(pid), sig); killed = True
            except Exception:
                try: os.kill(pid, sig); killed = True
                except Exception: pass
    try:
        d.update(state='failed', error='cancelled')
        tmp = p + '.tmp'; json.dump(d, open(tmp, 'w')); os.replace(tmp, p)
    except Exception: pass
    return killed
