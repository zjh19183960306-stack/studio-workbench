"""Single studio consumer for the existing FaceFusion/DFL and DreamID queues."""
import fcntl, json, os, shutil, subprocess, tarfile, threading, time
from pathlib import Path
from worker_common import ID, WAIT, atomic, update

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SSH = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', '-o', 'ServerAliveInterval=15',
       '-o', 'ServerAliveCountMax=2', '-i', str(HERE/'faceswap_probe_key'),
       '-p', '26958', 'root@connect.westd.seetacloud.com']
PREFIX = 'faceswap-worker'
OUT = ROOT/'outputs'/'faceswap'
STATUS = HERE/'faceswap_worker_status.json'

class WorkerError(Exception): pass
class TransportError(WorkerError): pass

def rpc(action, jid='', slot='', payload=None, source=None, dest=None, timeout=60):
    command = ' '.join(x for x in (PREFIX, action, jid, slot) if x)
    inp = source.open('rb') if source else subprocess.PIPE
    out = dest.open('wb') if dest else subprocess.PIPE
    try:
        with subprocess.Popen(SSH+[command], stdin=inp, stdout=out,
                              stderr=subprocess.DEVNULL) as p:
            try:
                b, _ = p.communicate(json.dumps(payload).encode() if payload is not None else None,
                                     timeout=timeout)
            except subprocess.TimeoutExpired:
                p.kill(); p.wait()
                raise TransportError('跨机连接或传送超时；队列记录已保留。')
            if p.returncode:
                raise TransportError('引擎机连接或文件传送失败；请检查目标机是否开机及探针密钥。')
        if dest: return None
        return json.loads(b)
    except (OSError, ValueError):
        raise TransportError('跨机连接失败或执行器返回无效；队列记录已保留。')
    finally:
        if source: inp.close()
        if dest: out.close()

def persist(p, d): atomic(p, d)

def allowed_file(value, kind):
    if not isinstance(value, str): raise WorkerError('任务缺少所选输入文件。')
    p = Path(value).resolve()
    roots = [ROOT/'videos'] if kind == 'video' else [ROOT/'inputs', ROOT/'ComfyUI'/'input']
    suffixes = {'.mp4','.mov','.mkv','.avi','.webm','.m4v'} if kind == 'video' else {'.png','.jpg','.jpeg','.webp'}
    if not any(p.is_relative_to(r.resolve()) for r in roots) or not p.is_file() or p.suffix.lower() not in suffixes:
        raise WorkerError('所选视频或人脸文件已不存在，或路径不在允许的素材目录。')
    return p

def transfer(p, d):
    videos = [allowed_file(v, 'video') for v in d.get('videos', [])]
    faces = [allowed_file(v, 'face') for v in d.get('faces', [])]
    engine = d.get('engine', 'dreamidv')
    if not 1 <= len(videos) <= 100 or len(faces) > 10 or (engine != 'deepfacelab' and not faces):
        raise WorkerError('任务缺少视频或人脸参考文件。')
    spec = {'id':d['id'], 'engine':engine, 'action':d.get('action','swap'),
            'videos':['v'+str(i)+v.suffix.lower() for i,v in enumerate(videos)],
            'faces':['f'+str(i)+v.suffix.lower() for i,v in enumerate(faces)],
            'model':Path(d.get('model_path') or 'unused').name,
            'model_class':d.get('model_class','Quick96'),
            'training_iterations':d.get('training_iterations',1000)}
    if engine == 'deepfacelab' and spec['action']=='train':
        material = Path(d.get('material_pack','')).resolve()
        if not any(material.is_relative_to(r.resolve()) for r in (ROOT/'inputs', ROOT/'zips', ROOT/'outputs')) or not material.exists():
            raise WorkerError('训练素材包已不存在或路径无效。')
        spec['material'] = 'material.zip' if material.is_file() and material.suffix.lower()=='.zip' else 'material.tar'
    if engine == 'dreamidv':
        spec['reference_policy'] = 'first'  # DreamID CLI accepts one image, disclosed in UI.
    init = rpc('init', d['id'], payload=spec)
    if not init.get('gpu'): return False
    for slot, src in list(zip(spec['videos'], videos)) + list(zip(spec['faces'], faces)):
        rpc('upload', d['id'], slot, source=src, timeout=1800)
    if spec.get('material'):
        if spec['material']=='material.zip': rpc('upload', d['id'], 'material.zip', source=material, timeout=1800)
        else:
            # Only executed after two positive GPU gates. Never unpack on studio.
            tmp = HERE/'faceswap_transfer'; tmp.mkdir(exist_ok=True, mode=0o700)
            archive = tmp/(d['id']+'.tar')
            try:
                total = sum(f.stat().st_size for f in material.rglob('*') if f.is_file() and not f.is_symlink())
                if total > shutil.disk_usage(tmp).free - 512*1024**2:
                    raise WorkerError('工作台剩余空间不足以传送训练素材包，请先释放空间。')
                with tarfile.open(archive, 'w') as t:
                    for f in material.rglob('*'):
                        if f.is_file() and not f.is_symlink(): t.add(f, arcname=str(f.relative_to(material)), recursive=False)
                rpc('upload', d['id'], 'material.tar', source=archive, timeout=1800)
            finally: archive.unlink(missing_ok=True)
    return True

def collect(p, d, result):
    dest = OUT/d['id']; dest.mkdir(parents=True, exist_ok=True, mode=0o700)
    urls=[]
    for slot in result.get('outputs', []):
        if not __import__('re').fullmatch(r'result-\d+\.(mp4|json)', slot):
            raise WorkerError('结果文件清单无效。')
        size=result.get('output_sizes',{}).get(slot,0)
        if type(size) is not int or size<0: raise WorkerError('结果文件大小记录无效。')
        if shutil.disk_usage(dest).free<size+512*1024**2:
            raise WorkerError('工作台空间不足，结果仍保留在引擎机；原队列记录已保留。')
        partial = dest/(slot+'.part')
        rpc('download', d['id'], slot, dest=partial, timeout=1800)
        if not partial.stat().st_size: raise WorkerError('引擎返回空结果文件。')
        os.replace(partial, dest/slot)
        os.chmod(dest/slot, 0o600)
        urls.append('/api/'+('dreamid' if d.get('engine')=='dreamidv' else 'faceswap')+'/jobs/'+d['id']+'/results/'+slot)
    if not urls and not result.get('model_ready'): raise WorkerError('引擎未生成结果，任务未标完成。')
    update(d, 'done', result.get('message','执行完成，结果已回传工作台。'), 5,
           results=urls, model_ready=result.get('model_ready',False), finished_at=time.time())
    persist(p,d)

def advance(p, d, probe):
    if d['state'] in ('queued','waiting_gpu','waiting_start'):
        if not probe.get('gpu'):
            msg = WAIT if probe.get('reachable') else '已排队，暂时无法连接引擎机；恢复连接并带卡开机后自动执行。'
            update(d,'waiting_gpu',msg,0,inference_started=False,training_started=False,environment=probe)
            persist(p,d); return
        if not probe.get('idle',True):
            update(d,'queued','显卡正在处理其他任务，已排队等待空闲。'); persist(p,d); return
        update(d,'staging','已检测到显卡，正在传送所选素材。',1,environment=probe)
        persist(p,d)
        if not transfer(p,d):
            update(d,'waiting_gpu',WAIT); persist(p,d); return
        result = rpc('start',d['id'])
        if result.get('state')=='waiting_gpu':
            update(d,'waiting_gpu',WAIT); persist(p,d); return
        if result.get('state')=='busy':
            update(d,'queued','显卡正在处理其他任务，等待空闲。'); persist(p,d); return
        update(d,'running','引擎已接收，正在准备 GPU 执行。',2,remote_started=True)
        persist(p,d)
    elif d['state']=='staging':
        # Recover a lost SSH start response without repeating training.
        result=rpc('status', d['id'])
        if result.get('state')=='missing' or result.get('state')=='staged':
            raise WorkerError('素材传送中断，未启动引擎；请重新提交任务，原记录已保留。')
    result = rpc('status',d['id'])
    if result.get('state')=='done':
        update(d,'collecting','引擎完成，正在回传结果。',4); persist(p,d); collect(p,d,result)
    elif result.get('state')=='failed':
        raise WorkerError(result.get('message','引擎执行失败。'))
    elif result.get('state')=='waiting_gpu':
        update(d,'waiting_gpu',WAIT,0); persist(p,d)
    elif result.get('state')=='running':
        update(d,'running',result.get('message','引擎执行中。'),result.get('step',3),
               progress=result.get('progress'), inference_started=result.get('inference_started',False),
               training_started=result.get('training_started',False),remote_started=True)
        persist(p,d)
    else: raise WorkerError('引擎执行记录缺失或意外退出，任务保留，请重新提交。')

def tick():
    try: probe = rpc('probe')
    except WorkerError: probe={'gpu':False,'reachable':False}
    active=[]; waiting=[]
    for name in ('faceswap_queue','dreamid_queue'):
        for p in (HERE/name).glob('*.json'):
            if not ID.fullmatch(p.stem): continue
            try:
                d=json.loads(p.read_text(encoding='utf-8'))
                if d.get('id')!=p.stem: continue
                if name=='dreamid_queue': d['engine']='dreamidv'
                if d.get('state') in ('staging','running','collecting'): active.append((p,d))
                elif d.get('state') in ('queued','waiting_gpu','waiting_start'): waiting.append((p,d))
            except (OSError,ValueError): continue
    # Update all waiting jobs when there is no GPU, without touching input bytes.
    jobs = active[:1] if active else sorted(waiting,key=lambda x:x[1].get('created_at',0))
    if probe.get('gpu'): jobs=jobs[:1]
    for p,d in jobs:
        try: advance(p,d,probe)
        except WorkerError as e:
            # Network loss while executing: resume status polling, don't lose remote work.
            if d.get('remote_started') and isinstance(e,TransportError):
                update(d,'running','引擎机连接暂时中断，正在重连确认执行状态。',d.get('step',3))
            else: update(d,'failed',str(e),d.get('step',0),finished_at=time.time())
            persist(p,d)
        except Exception:
            update(d,'failed','执行器发生内部错误；原队列记录已保留，请检查部署。',d.get('step',0))
            persist(p,d)
    return probe, len(active), len(waiting)

def main():
    os.umask(0o077)
    lock=open(HERE/'faceswap_worker.lock','a')
    try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError: return
    def heartbeat():
        while True:
            atomic(STATUS,{'pid':os.getpid(),'heartbeat':time.time(),'automatic_worker':True})
            time.sleep(5)
    threading.Thread(target=heartbeat,daemon=True).start()
    while True:
        try: tick()
        except Exception: pass  # No raw exceptions: may contain user paths.
        time.sleep(5)

if __name__=='__main__': main()
