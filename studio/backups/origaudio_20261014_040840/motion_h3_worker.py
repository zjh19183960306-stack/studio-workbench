"""MODULE A worker: motion_generate_queue -> local H3 (ComfyUI :8192, GPU only).
Appearance/clothes only from person image; control video (depth/white preview)
supplies motion only. No cloud API. Never logs media names or paths."""
import fcntl, json, os, time, threading, subprocess, shutil, uuid, traceback
from pathlib import Path
import requests, websocket
import motion_preview as m
import motion_h3_bridge as bridge
from worker_common import atomic, ID, gpu_present
HERE = Path(__file__).resolve().parent
R = HERE.parent
URL = 'http://127.0.0.1:8192'
WS = 'ws://127.0.0.1:8192/ws?clientId='
STATUS = HERE/'motion_h3_worker_status.json'
WORKFLOW = R/'char_anim_h3'/'r2v_api.json'
INPUT_SUB = '_h3work'            # skipped by char_anim.catalog
OUT_ROOT = HERE/'outputs'/'motion_generate'
STEPS = ['准备', '加载模型', '采样', '解码', '保存', '完成']
LOG = R/'motion_h3_worker.log'

def log(msg):
    with open(LOG, 'a', encoding='utf-8') as f: f.write(time.strftime('%F %T ')+msg+'\n')

def weights_ok():
    try:
        v = json.loads((R/'char_anim_h3'/'verified.json').read_text())
        return all(k in v and Path(v[k]['path']).is_file() and Path(v[k]['path']).stat().st_size == v[k]['size'] for k in ('vv','av','te','ref'))
    except (OSError, ValueError, KeyError): return False

def comfy_stats():
    try: return requests.get(URL+'/system_stats', timeout=5).json()
    except (requests.RequestException, ValueError): return None

def comfy_idle():
    try:
        q = requests.get(URL+'/queue', timeout=5).json()
        return not q.get('queue_running') and not q.get('queue_pending')
    except (requests.RequestException, ValueError): return False

class Adapter:
    """Local-only adapter for motion_h3_bridge.dispatch_one."""
    def probe(self):
        s = comfy_stats(); cuda = bool(s and any(d.get('type') == 'cuda' for d in s.get('devices', [])))
        return {'gpu': gpu_present() and cuda, 'ready': weights_ok(), 'idle': cuda and comfy_idle(),
                'control_video_input': True, 'engine': 'minimax_h3_ref2va_local'}
    def enqueue(self, manifest):
        # Durable + idempotent: the queue json itself is the record (keyed by id).
        return {'accepted': manifest.get('engine') == 'minimax_h3_ref2va_local'
                and manifest.get('appearance_source') == 'images_only'
                and manifest.get('motion_source') == 'approved_controls_only'}

def setp(p, d, **kw):
    d.update(updated_at=time.time(), steps=STEPS, **kw); atomic(p, d)

def upload(path, name, folder):
    with open(path, 'rb') as f:
        r = requests.post(URL+'/upload/image', files={'image': (name, f, 'application/octet-stream')},
                          data={'type': 'input', 'subfolder': folder, 'overwrite': 'true'}, timeout=600)
    r.raise_for_status(); d = r.json(); return d['subfolder']+'/'+d['name']

def probe_video(path):
    r = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-count_packets',
                        '-show_entries', 'stream=width,height,nb_read_packets,r_frame_rate', '-of', 'json', str(path)],
                       capture_output=True, text=True, timeout=120)
    s = json.loads(r.stdout)['streams'][0]
    num, den = (s.get('r_frame_rate') or '24/1').split('/'); fps = float(num)/float(den or 1)
    return int(s['width']), int(s['height']), int(s['nb_read_packets']), fps

def plan(w, h, frames, fps):
    secs = frames/max(fps, 1)
    n = int(min(max(secs, 2.0), 15.0)*24)
    n = min(n, 362); n = max(5, 5+((n-5)//17)*17)
    scale = (640*640/(w*h))**0.5
    W = max(256, int(w*scale)//32*32); H = max(256, int(h*scale)//32*32)
    return W, H, n

PROMPT = ("Use <Picture 1> as the only source for the character identity, face, hair, body proportions, clothes, colors and accessories. "
          "Animate that exact character. <Video 1> is an abstract motion-control video ({kind}); use it only for movement, timing and pose. "
          "Never copy appearance, body shape, clothes, face, textures or background from <Video 1>. Render the character from <Picture 1>. "
          "Stable outfit throughout, neutral background, fixed camera, no speech.")
ANTI = (" Strict anti-bleed: the outfit, colors and accessories must exactly match <Picture 1> in every frame; "
        "do not invent, add or swap any clothing; ignore all shapes, silhouettes and shading of <Video 1> except the motion.")

def graph(img, vid, prefix, W, H, n, kind, anti, seed):
    g = json.loads(WORKFLOW.read_text(encoding='utf-8'))
    g['6']['inputs']['image'] = img; g['7']['inputs']['file'] = vid
    k = 'depth map' if kind == 'depth' else 'gray silhouette'
    g['9']['inputs'].update(prompt=PROMPT.format(kind=k)+(ANTI if anti else ''), width=W, height=H, length=n)
    i9 = g['9']['inputs']
    for a, b in (('ref_image_0', 'ref_images.ref_image_0'), ('ref_video_0', 'ref_videos.ref_video_0')):
        if a in i9: i9[b] = i9.pop(a)
    g['10']['inputs']['noise_seed'] = seed
    g['18']['inputs']['filename_prefix'] = prefix
    return g

NODE_STEP = {'2': '加载模型', '3': '加载模型', '4': '加载模型', '5': '加载模型', '9': '编码参考',
             '14': '采样', '15': '解码', '16': '解码', '17': '保存', '18': '保存'}

def run_one(p, d, idx, total, img_path, ctl, out_dir):
    cid = uuid.uuid4().hex; folder = INPUT_SUB+'/'+d['id']
    base_pct = idx*100.0/total; span = 100.0/total
    def prog(stage, frac, extra=''):
        pct = int(base_pct+span*frac); pct = min(pct, 99)
        label = f'第 {idx+1}/{total} 条' if total > 1 else ''
        setp(p, d, state='running', inference_started=True, stage=stage, progress=pct,
             message=('生成中 '+label+' · '+stage+(' '+extra if extra else '')+f' · {pct}%').replace('  ', ' '))
    prog('准备', 0.01)
    W, H, n = plan(*probe_video(ctl['path']))
    ci = upload(img_path, 'person'+Path(img_path).suffix.lower(), folder)
    vi = upload(ctl['path'], 'control.mp4', folder)
    prefix = 'motion_generate_h3/'+d['id']+'/r'+str(idx)
    ws = websocket.create_connection(WS+cid, timeout=30)
    try:
        r = requests.post(URL+'/prompt', json={'prompt': graph(ci, vi, prefix, W, H, n, ctl['kind'], d['anti_bleed'], 123456+idx), 'client_id': cid}, timeout=60)
        if r.status_code != 200:
            log('prompt rejected '+str(r.status_code)+' '+r.text[:800]); raise RuntimeError('E_H3_PROMPT_REJECTED')
        pid = r.json()['prompt_id']; prog('加载模型', 0.03)
        stage = '加载模型'; ws.settimeout(20); deadline = time.time()+4*3600; last = 0
        while time.time() < deadline:
            try: raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                if not gpu_present(): raise RuntimeError('E_H3_GPU_LOST')
                h = requests.get(URL+'/history/'+pid, timeout=15).json().get(pid)
                if h and h.get('status', {}).get('completed'): break
                if h and h.get('status', {}).get('status_str') == 'error': raise RuntimeError('E_H3_EXECUTION')
                continue
            if not isinstance(raw, str): continue
            msg = json.loads(raw); t = msg.get('type'); data = msg.get('data', {})
            if data.get('prompt_id') not in (None, pid): continue
            if t == 'executing':
                node = data.get('node')
                if node is None: break
                stage = NODE_STEP.get(str(node), stage)
                frac = {'加载模型': 0.05, '编码参考': 0.08, '采样': 0.10, '解码': 0.90, '保存': 0.97}.get(stage, 0.05)
                prog(stage, frac)
            elif t == 'progress' and stage == '采样' and time.time()-last > 2:
                v, mx = int(data.get('value', 0)), int(data.get('max', 1)) or 1
                last = time.time(); prog('采样', 0.10+0.80*v/mx, f'{v}/{mx} 步')
            elif t == 'execution_error':
                log('execution_error '+json.dumps({k: data.get(k) for k in ('node_id', 'node_type', 'exception_type', 'exception_message')}, ensure_ascii=False)[:1500])
                raise RuntimeError('E_H3_EXECUTION')
            elif t == 'execution_interrupted': raise RuntimeError('E_H3_INTERRUPTED')
        else:
            requests.post(URL+'/interrupt', timeout=10); raise RuntimeError('E_H3_TIMEOUT')
        h = requests.get(URL+'/history/'+pid, timeout=15).json()[pid]
        if h.get('status', {}).get('status_str') == 'error': raise RuntimeError('E_H3_EXECUTION')
        o = h['outputs']['18']; files = o.get('images', [])+o.get('videos', [])+o.get('gifs', [])
        f = next((x for x in files if x['filename'].endswith('.mp4')), None)
        if not f: raise RuntimeError('E_H3_NO_OUTPUT')
        src = (R/'outputs'/f.get('subfolder', '')/f['filename']).resolve()
        if not src.is_relative_to((R/'outputs'/'motion_generate_h3').resolve()): raise RuntimeError('E_H3_OUTPUT_PATH')
        slot = 'result-'+str(idx)+'.mp4'; shutil.move(str(src), str(out_dir/slot)); return slot
    finally:
        ws.close()
        shutil.rmtree(R/'inputs'/INPUT_SUB/d['id'], ignore_errors=True)

def process(p):
    d = json.loads(p.read_text(encoding='utf-8'))
    if d.get('state') not in ('queued', 'running'): return False
    try:
        controls = m.resolve_controls(d['owner'], d['motion_ids']); c = m.catalog()
        images = [x['path'] for x in m.resolve_images(d['owner'], d['image_ids'])]
        if len(images) != len(d['image_ids']) or not controls: raise RuntimeError('E_H3_INPUT_MISSING')
        out = OUT_ROOT/d['id']; out.mkdir(parents=True, exist_ok=True, mode=0o700)
        pairs = [(im, ct) for im in images for ct in controls]; slots = []
        for i, (im, ct) in enumerate(pairs): slots.append(run_one(p, d, i, len(pairs), im, ct, out))
        shutil.rmtree(R/'outputs'/'motion_generate_h3'/d['id'], ignore_errors=True)
        urls = ['/api/motion-preview/generation-jobs/'+d['id']+'/results/'+s for s in slots]
        setp(p, d, state='done', stage='完成', progress=100, message=f'出片完成 · 共 {len(slots)} 条 · 100%', results=urls, output_slots=slots)
        log('job '+d['id']+' done slots='+str(len(slots)))
    except Exception as e:
        code = str(e) if str(e).startswith('E_H3_') else 'E_H3_'+type(e).__name__.upper()
        log('job '+d['id']+' failed '+code+'\n'+traceback.format_exc()[-1500:])
        setp(p, d, state='failed', stage='失败', message='出片失败（错误码 '+code+'），可重新提交')
    return True

def tick():
    a = Adapter(); pr = a.probe()
    for p in sorted(m.GENERATE.glob('*.json')):
        if not ID.fullmatch(p.stem): continue
        try: d = json.loads(p.read_text(encoding='utf-8'))
        except (OSError, ValueError): continue
        st = d.get('state')
        if st == 'running':  # crashed mid-run -> retry once from queued
            if d.get('_retried'): setp(p, d, state='failed', message='出片中断（错误码 E_H3_WORKER_RESTART），可重新提交'); continue
            setp(p, d, state='queued', _retried=True, message='上次中断，重新排队'); st = 'queued'
        if st == 'engine_pending':
            if not pr['gpu']: setp(p, d, message='已排队：需要带卡开机（H3 模型已就绪）' if pr['ready'] else '已排队：H3 模型未齐'); continue
            if not pr['ready']: setp(p, d, message='已排队：H3 模型校验未通过'); continue
            if not pr['idle']: setp(p, d, message='已排队：显卡正忙，等待空闲'); continue
            bridge.dispatch_one(a, p.stem)
            d = json.loads(p.read_text(encoding='utf-8')); st = d.get('state')
        if st == 'queued' and pr['gpu'] and pr['idle']:
            process(p); return

def main():
    os.umask(0o077); lock = open(HERE/'motion_h3_worker.lock', 'a')
    try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError: return
    def pulse():
        while True: atomic(STATUS, {'pid': os.getpid(), 'heartbeat': time.time()}); time.sleep(5)
    threading.Thread(target=pulse, daemon=True).start()
    log('worker started')
    while True:
        try: tick()
        except Exception: log('tick error '+traceback.format_exc()[-800:])
        time.sleep(5)

if __name__ == '__main__': main()
