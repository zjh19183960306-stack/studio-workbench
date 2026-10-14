"""Validated submissions into the existing automatic GPU worker queue."""
import asyncio, json, os, secrets, time, re
from pathlib import Path
from aiohttp import web
import dreamid
from worker_common import WAIT, summary
import worker_api
HERE = Path(__file__).resolve().parent
QUEUE = HERE/'faceswap_queue'
REMOTE = ['ssh','-o','BatchMode=yes','-o','ConnectTimeout=8','-i',str(HERE/'faceswap_probe_key'),'-p','26958','root@connect.westd.seetacloud.com','/root/miniconda3/bin/python /root/autodl-tmp/faceswap_probe.py']
_probe_cache=None
_probe_lock=asyncio.Lock()

async def environment():
    global _probe_cache
    if _probe_cache and time.time()-_probe_cache['checked_at']<30: return dict(_probe_cache)
    async with _probe_lock:
        if _probe_cache and time.time()-_probe_cache['checked_at']<30: return dict(_probe_cache)
        d=await _probe_remote()
        if d.get('reachable'): _probe_cache=d
        return dict(d)

async def _probe_remote():
    try:
        proc=await asyncio.create_subprocess_exec(*REMOTE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
        try: out,_=await asyncio.wait_for(proc.communicate(),45)
        except asyncio.TimeoutError:
            proc.kill(); await proc.wait(); raise ValueError()
        if proc.returncode: raise ValueError()
        d=json.loads(out); d['checked_at']=time.time(); return d
    except (OSError,ValueError):
        return {'reachable':False,'gpu':False,'ready':False,'message':'安装机暂时无法探测，任务仅等待；不会启动训练或换脸。','checked_at':time.time()}

def public(d): return summary(d)

async def env(request): return web.json_response(await environment())

async def submit(request):
    try:
        import pack, video
        data=await request.json(); engine=data.get('engine'); action=data.get('action','swap')
        if engine not in ('facefusion','deepfacelab') or action not in ('train','swap') or (engine=='facefusion' and action!='swap'): raise ValueError()
        vs=data.get('videos',[]); fs=data.get('faces',[])
        if not isinstance(vs,list) or not 1<=len(vs)<=100 or not isinstance(fs,list) or len(fs)>10: raise ValueError()
        videos=[dreamid.checked(video.VIDEOS,n,{'.mp4','.mov','.mkv','.avi','.webm','.m4v'}) for n in vs]
        faces=[]
        for f in fs:
            if not isinstance(f,dict): raise ValueError()
            if f.get('lib'): faces.append(dreamid.checked(pack.INPUTS,f['lib'],{'.png','.jpg','.jpeg','.webp'}))
            else:
                ref=request.app['references'].get(f.get('id'))
                if not ref or ref[0]!=request['owner']: raise ValueError()
                faces.append(dreamid.checked(Path('/root/autodl-tmp/ComfyUI/input'),ref[1],{'.png','.jpg','.jpeg','.webp'}))
        if engine=='facefusion' and not faces: raise ValueError()
        material=data.get('material_pack',''); model=data.get('model','')
        if engine=='deepfacelab':
            if action=='train':
                if data.get('confirm_train') is not True: raise ValueError()
                if isinstance(material,str) and re.fullmatch(r'\d{8}-\d{6}',material):
                    source=(pack.OUTPUTS/material).resolve()
                    if not source.is_relative_to(pack.OUTPUTS.resolve()) or not source.is_dir(): raise ValueError()
                    material=str(source)
                else:
                    try: material=dreamid.checked(pack.ZIPS,material,{'.zip'})
                    except ValueError: material=dreamid.checked(pack.INPUTS,material,{'.zip'})
            if not isinstance(model,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',model): raise ValueError()
        probe=await environment()
        # Only stat a selected model directory; never enumerate user model names or contents.
        if engine=='deepfacelab' and action=='swap':
            if not probe.get('reachable'): return web.json_response({'error':'无法连接安装机，不能校验已有模型，请稍后再试'},status=503)
            cmd=['ssh','-o','BatchMode=yes','-o','ConnectTimeout=8','-i',str(HERE/'faceswap_probe_key'),'-p','26958','root@connect.westd.seetacloud.com','test -d /root/autodl-tmp/deepfacelab/workspace/models/'+model]
            p=await asyncio.create_subprocess_exec(*cmd,stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
            try: rc=await asyncio.wait_for(p.wait(),12)
            except asyncio.TimeoutError: p.kill(); await p.wait(); rc=1
            if rc: return web.json_response({'error':'所选模型目录不存在，请先准备并训练专属模型'},status=400)
        QUEUE.mkdir(exist_ok=True,mode=0o700); os.chmod(QUEUE,0o700)
        if sum(1 for _ in QUEUE.glob('*.json'))>=1000: return web.json_response({'error':'队列已满'},status=429)
        message=WAIT
        if probe.get('gpu'): message='已排队，自动执行器会在显卡空闲时开工。'
        model_class=data.get('model_class','Quick96')
        iterations=data.get('training_iterations',1000)
        if model_class not in ('Quick96','SAEHD') or type(iterations) is not int or not 1<=iterations<=100000: raise ValueError()
        d={'id':secrets.token_hex(16),'owner':request['owner'],'created_at':time.time(),'engine':engine,'action':action,'state':'waiting_gpu' if not probe.get('gpu') else 'queued','video_count':len(videos),'face_count':len(faces),'videos':videos,'faces':faces,'material_pack':material,'model_path':('/root/autodl-tmp/deepfacelab/workspace/models/'+model) if model else '', 'environment':probe,'message':message,'inference_started':False,'training_started':False,'model_class':model_class,'training_iterations':iterations,'step':0,'steps':['排队与 GPU 检查','传送所选素材','引擎准备','执行训练或换脸','回传结果','完成']}
        fd=os.open(QUEUE/(d['id']+'.json'),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w',encoding='utf-8') as out: json.dump(d,out,ensure_ascii=False)
        return web.json_response(public(d),status=201)
    except (ValueError,TypeError,AttributeError,KeyError): return web.json_response({'error':'选择无效，请检查视频、人脸照片、素材包或模型编号；训练需要明确勾选确认。'},status=400)

async def jobs(request):
    result=[]
    if QUEUE.exists():
        for p in QUEUE.glob('*.json'):
            try:
                d=json.loads(p.read_text(encoding='utf-8'))
                if d['owner']==request['owner']: result.append(public(d))
            except (OSError,ValueError,KeyError): pass
    return web.json_response({'jobs':sorted(result,key=lambda d:d['created_at'],reverse=True)[:100]})

def setup(app):
    worker_api.setup(app,QUEUE,'/api/faceswap')
    app.add_routes([web.get('/api/faceswap/environment',env),web.post('/api/faceswap/jobs',submit),web.get('/api/faceswap/jobs',jobs)])
