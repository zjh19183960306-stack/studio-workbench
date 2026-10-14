"""Opaque library IDs only; waiting submissions never read media bytes."""
import asyncio, hashlib, json, secrets, time
from pathlib import Path
from aiohttp import web
from worker_common import atomic, ID
import char_anim_worker as worker
HERE=Path(__file__).resolve().parent; QUEUE=HERE/'char_anim_queue'
CAT=HERE/'char_anim_catalog.json'
POLICY='衣服与外形只跟所选角色图；驱动片只提供动作，不会保留驱动片衣服。'
def public(d):
    return {k:d[k] for k in ('id','created_at','state','message','step','progress','results','anti_bleed','inference_started') if k in d}
def catalog():
    result={}
    for kind,root,ext in [('image',HERE.parent/'inputs',{'.png','.jpg','.jpeg','.webp'}),('video',HERE.parent/'videos',{'.mp4','.mov','.mkv','.webm','.avi','.m4v'})]:
        if not root.exists():continue
        for p in root.rglob('*'):
            if p.is_symlink() or not p.is_file() or p.suffix.lower() not in ext:continue
            rel=str(p.relative_to(root)); key=hashlib.sha256((kind+'\0'+rel).encode()).hexdigest()[:24]
            result[key]={'kind':kind,'path':str(p)}
    atomic(CAT,result);return result
async def materials(request):
    return web.json_response({'items':[{'id':i,'kind':v['kind']} for i,v in catalog().items()]})
async def submit(request):
    try:
        a=await request.json(); imgs=a['image_ids'];vid=a['video_id'];anti=a.get('anti_bleed',False)
        if type(anti)!=bool or not isinstance(imgs,list) or not 1<=len(imgs)<=8 or len(set(imgs))!=len(imgs):raise ValueError()
        c=json.loads(CAT.read_text(encoding='utf-8'))
        if any(c[x]['kind']!='image' for x in imgs) or c[vid]['kind']!='video':raise ValueError()
        d={'id':secrets.token_hex(16),'owner':request['owner'],'created_at':time.time(),'state':'waiting_gpu','message':'已排队，需要带卡开机','image_ids':imgs,'video_id':vid,'anti_bleed':anti,'inference_started':False,'step':0}
        atomic(QUEUE/(d['id']+'.json'),d);return web.json_response(public(d),status=201)
    except (KeyError,TypeError,ValueError,OSError):return web.json_response({'error':'请选择有效的角色素材 id 和视频 id'},status=400)
def listing(owner):
    values=[]
    for p in QUEUE.glob('*.json'):
        try:
            d=json.loads(p.read_text(encoding='utf-8'))
            if d['owner']==owner:values.append(public(d))
        except (ValueError,OSError,KeyError):pass
    return sorted(values,key=lambda d:d['created_at'],reverse=True)[:100]
async def jobs(r):return web.json_response({'jobs':listing(r['owner'])})
async def events(r):
    s=web.StreamResponse(headers={'Content-Type':'text/event-stream','Cache-Control':'no-cache','X-Accel-Buffering':'no'});await s.prepare(r)
    try:
        for _ in range(150):
            await s.write(('data: '+json.dumps({'jobs':listing(r['owner'])},ensure_ascii=False)+'\n\n').encode());await asyncio.sleep(2)
    except (ConnectionResetError,asyncio.CancelledError):pass
    return s
async def result(r):
    jid=r.match_info['jid'];slot=r.match_info['slot']
    if not ID.fullmatch(jid) or not __import__('re').fullmatch(r'result-\d+\.mp4',slot):raise web.HTTPNotFound()
    try:d=json.loads((QUEUE/(jid+'.json')).read_text())
    except (OSError,ValueError):raise web.HTTPNotFound()
    if d['owner']!=r['owner'] or d['state']!='done' or slot not in d.get('output_slots',[]):raise web.HTTPNotFound()
    return web.FileResponse(HERE.parent/'outputs'/'char_anim'/jid/slot,headers={'Cache-Control':'private, no-store'})
def setup(app):
    QUEUE.mkdir(exist_ok=True,mode=0o700)
    app.add_routes([web.get('/api/char-anim/materials',materials),web.post('/api/char-anim/jobs',submit),web.get('/api/char-anim/jobs',jobs),web.get('/api/char-anim/events',events),web.get('/api/char-anim/jobs/{jid}/results/{slot}',result)])
