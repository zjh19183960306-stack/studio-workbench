"""DreamID validated submissions; automatic worker shares studio GPU lease."""
import json, time, secrets, os
from pathlib import Path
from aiohttp import web
from worker_common import WAIT, summary
import worker_api
HERE = Path(__file__).resolve().parent
QUEUE = HERE/'dreamid_queue'
MESSAGE = WAIT

def environment():
    try:
        d = json.loads((HERE/'dreamid_environment.json').read_text(encoding='utf-8-sig'))
        d['stale'] = time.time()-d.get('checked_at',0)>600
        return d
    except (OSError, ValueError):
        return {'ready':False,'stale':True,'message':'还没有环境检查记录'}

def checked(root, name, extensions):
    if not isinstance(name,str) or not name or '\\' in name:
        raise ValueError('所选文件路径无效')
    p = (root/name).resolve()
    if not p.is_relative_to(root.resolve()) or not p.is_file() or p.suffix.lower() not in extensions:
        raise ValueError('所选文件不存在或类型不支持')
    return str(p)

def public(d): return summary(d)

async def env(request):
    return web.json_response(environment())

async def submit(request):
    try:
        import pack, video
        data = await request.json()
        vs, fs = data.get('videos'), data.get('faces')
        if not isinstance(vs,list) or not 1<=len(vs)<=100 or not isinstance(fs,list) or not 1<=len(fs)<=10:
            raise ValueError('请勾选 1 到 100 个视频，并选择 1 到 10 张人脸参考照片')
        videos=[checked(video.VIDEOS,n,{'.mp4','.mov','.mkv','.avi','.webm','.m4v'}) for n in vs]
        faces=[]
        for f in fs:
            if not isinstance(f,dict): raise ValueError('参考照片无效')
            if f.get('lib'):
                faces.append(checked(pack.INPUTS,f['lib'],{'.png','.jpg','.jpeg','.webp'}))
            else:
                ref=request.app['references'].get(f.get('id'))
                if not ref or ref[0]!=request['owner']: raise ValueError('上传照片已失效，请重新上传')
                faces.append(checked(Path('/root/autodl-tmp/ComfyUI/input'),ref[1],{'.png','.jpg','.jpeg','.webp'}))
        QUEUE.mkdir(exist_ok=True,mode=0o700)
        if len(list(QUEUE.glob('*.json')))>=1000: raise ValueError('队列已满，请先处理已有任务')
        d={'id':secrets.token_hex(16),'owner':request['owner'],'created_at':time.time(),
           'engine':'dreamidv','action':'swap','state':'waiting_gpu','step':0,'steps':['排队与 GPU 检查','传送所选素材','引擎准备','执行训练或换脸','回传结果','完成'],
           'video_count':len(videos),'face_count':len(faces),'videos':videos,'faces':faces,
           'environment':environment(),'message':MESSAGE,'inference_started':False}
        p=QUEUE/(d['id']+'.json')
        with open(p,'x',encoding='utf-8') as out: json.dump(d,out,ensure_ascii=False)
        os.chmod(p,0o600)
        return web.json_response(public(d),status=201)
    except (ValueError,TypeError,AttributeError):
        return web.json_response({'error':'选择无效：请确认视频和参考照片存在，且上传未失效。'},status=400)

async def jobs(request):
    result=[]
    if QUEUE.exists():
        for p in QUEUE.glob('*.json'):
            try:
                d=json.loads(p.read_text(encoding='utf-8'))
                if d['owner']==request['owner']: result.append(public(d))
            except (OSError,ValueError,KeyError): pass
    return web.json_response({'jobs':sorted(result,key=lambda x:x['created_at'],reverse=True)[:100]})

def setup(app):
    worker_api.setup(app,QUEUE,'/api/dreamid')
    app.add_routes([web.get('/api/dreamid/environment',env),web.post('/api/dreamid/jobs',submit),web.get('/api/dreamid/jobs',jobs)])
