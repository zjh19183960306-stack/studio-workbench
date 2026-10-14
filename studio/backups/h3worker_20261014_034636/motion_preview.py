"""Motion-only contract. No model downloads, RGB forwarding or cloud API."""
import shutil
import asyncio, json, secrets, time, shutil
from pathlib import Path
from aiohttp import web
from urllib.parse import quote
import char_anim
from worker_common import atomic, ID
HERE = Path(__file__).resolve().parent
QUEUE = HERE/'motion_preview_queue'
GENERATE = HERE/'motion_generate_queue'
OUT = HERE/'outputs'/'motion_preview'
UPLOAD = HERE/'motion_uploads'

def catalog():
    c = char_anim.catalog()
    for p in UPLOAD.glob('*.mp4'):
        if ID.fullmatch(p.stem) and not p.is_symlink():
            c[p.stem] = {'kind':'video', 'path':str(p)}
    return c

def public(d):
    return {k:d[k] for k in ('id','state','message','created_at','results','anti_bleed','engine','motion_ids','image_ids','inference_started','control_kind','video_id') if k in d}

def read_owned(root, jid, owner):
    if not isinstance(jid,str) or not ID.fullmatch(jid): raise ValueError('编号无效')
    try: d=json.loads((root/(jid+'.json')).read_text(encoding='utf-8'))
    except (OSError,ValueError): raise ValueError('编号无效')
    if d.get('owner')!=owner: raise ValueError('编号无效')
    return d

def listing(root, owner):
    items=[]
    for p in root.glob('*.json'):
        try:
            d=read_owned(root,p.stem,owner);items.append(public(d))
        except ValueError: pass
    return sorted(items,key=lambda x:x['created_at'],reverse=True)[:100]

async def materials(r):
    c=catalog()
    # Uploaded references are private to their session.
    items=[]
    for i,v in c.items():
        if i in {p.stem for p in UPLOAD.glob('*.mp4')}:
            try:
                if json.loads((UPLOAD/(i+'.json')).read_text())['owner']!=r['owner']: continue
            except (OSError,ValueError,KeyError): continue
        item={'id':i,'kind':v['kind']}
        path=Path(v['path'])
        if v['kind']=='image':
            item['thumb_url']='/api/inputs/'+quote(path.relative_to(HERE.parent/'inputs').as_posix(),safe='/')+'?thumb=1'
        elif path.parent==HERE.parent/'videos' and path.suffix.lower()!='.avi':
            item['cover_url']='/api/videos/'+quote(path.name,safe='')+'/cover'
        else:
            item['cover_url']='/api/motion-preview/materials/'+i+'/cover'
        items.append(item)
    return web.json_response({'items':items})

async def material_cover(r):
    # Resolve opaque IDs only after the global authentication guard.
    mid=r.match_info['mid']; c=catalog(); v=c.get(mid)
    if not v or v['kind']!='video': raise web.HTTPNotFound()
    p=Path(v['path'])
    if p.parent==UPLOAD:
        try:
            if json.loads((UPLOAD/(mid+'.json')).read_text(encoding='utf-8'))['owner']!=r['owner']: raise web.HTTPNotFound()
        except (OSError,ValueError,KeyError): raise web.HTTPNotFound()
    import video
    async with video._cover_slots:
        dest=video._cover_path(p)
        if not dest.exists() or dest.stat().st_mtime<p.stat().st_mtime:
            dest.parent.mkdir(exist_ok=True)
            try: await asyncio.to_thread(video._make_cover,p,dest)
            except Exception: return web.json_response({'error':'封面暂不可用'},status=422)
    return web.FileResponse(dest,headers={'Cache-Control':'private, no-store'})

async def upload(r):
    if shutil.disk_usage(HERE).free<512*1024**2: raise ValueError('磁盘空间不足，请使用视频库编号')
    jid=secrets.token_hex(16);p=UPLOAD/(jid+'.mp4');size=0
    try:
        with p.open('xb') as f:
            async for chunk in r.content.iter_chunked(65536):
                size+=len(chunk)
                if size>20*1024**2: raise ValueError('上传上限为 20 MB，请使用视频库编号')
                f.write(chunk)
        if not size: raise ValueError('请选择参考视频')
        proc=await asyncio.create_subprocess_exec(shutil.which('ffprobe') or '/usr/bin/ffprobe','-v','error','-select_streams','v:0','-show_entries','stream=codec_type','-of','csv=p=0',str(p),stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
        try: stdout,_=await asyncio.wait_for(proc.communicate(),15)
        except asyncio.TimeoutError:
            proc.kill();await proc.wait();raise ValueError('参考视频校验超时')
        if proc.returncode or b'video' not in stdout: raise ValueError('参考视频格式无效')
        atomic(UPLOAD/(jid+'.json'),{'owner':r['owner']})
    except BaseException:
        p.unlink(missing_ok=True);raise
    return web.json_response({'id':jid,'kind':'video'},status=201)

async def submit(r):
    a=await r.json()
    if not isinstance(a,dict): raise ValueError('请求无效')
    depth=a.get('make_depth',False);white=a.get('make_white',False)
    if type(depth)!=bool or type(white)!=bool or not(depth or white): raise ValueError('请至少选择白模或深度空间一种')
    vid=a.get('video_id');c=catalog()
    if not isinstance(vid,str) or vid not in c or c[vid]['kind']!='video': raise ValueError('请选择有效的参考视频编号')
    if (UPLOAD/(vid+'.json')).exists():
        if json.loads((UPLOAD/(vid+'.json')).read_text())['owner']!=r['owner']: raise ValueError('编号无效')
    d={'id':secrets.token_hex(16),'owner':r['owner'],'created_at':time.time(),'video_id':vid,'make_depth':depth,'make_white':white,'state':'queued','message':'已排队：正在用深度/灰模引擎生成预演（CPU 可用，无需等 H3）','results':[]}
    atomic(QUEUE/(d['id']+'.json'),d)
    return web.json_response(public(d),status=201)

async def jobs(r): return web.json_response({'jobs':listing(QUEUE,r['owner']),'generation_jobs':listing(GENERATE,r['owner'])})
async def events(r):
    s=web.StreamResponse(headers={'Content-Type':'text/event-stream','X-Accel-Buffering':'no'});await s.prepare(r)
    try:
        for _ in range(150):
            await s.write(('data: '+json.dumps({'jobs':listing(QUEUE,r['owner']),'generation_jobs':listing(GENERATE,r['owner'])},ensure_ascii=False)+'\n\n').encode());await asyncio.sleep(2)
    except (ConnectionResetError,asyncio.CancelledError): pass
    return s

async def result(r):
    try: d=read_owned(QUEUE,r.match_info['jid'],r['owner'])
    except ValueError: raise web.HTTPNotFound()
    slot=r.match_info['slot']
    if d['state']!='done' or slot not in ('result-depth.mp4','result-white.mp4') or not any(x['slot']==slot for x in d['results']): raise web.HTTPNotFound()
    p=OUT/d['id']/slot
    if not p.is_file() or p.is_symlink(): raise web.HTTPNotFound()
    return web.FileResponse(p,headers={'Cache-Control':'private, no-store'})

def resolve_controls(owner,motion_ids):
    """MODULE A adapter: resolve only approved control output; never source RGB."""
    controls=[]
    for mid in motion_ids:
        if not isinstance(mid,str) or ':' not in mid: raise ValueError('预览编号无效')
        jid,kind=mid.split(':',1)
        if kind not in ('depth','white'): raise ValueError('预览编号无效')
        d=read_owned(QUEUE,jid,owner);slot='result-'+kind+'.mp4'
        if d['state']!='done' or not any(x['id']==mid for x in d['results']): raise ValueError('请勾选已完成且通过检查的预览')
        p=OUT/jid/slot
        if not p.is_file() or p.is_symlink(): raise ValueError('预览暂不可用')
        controls.append({'id':mid,'kind':kind,'path':str(p)})
    return controls

async def generate(r):
    a=await r.json()
    if not isinstance(a,dict): raise ValueError('请求无效')
    imgs=a.get('image_ids');mids=a.get('motion_ids');anti=a.get('anti_bleed',True)
    if type(anti)!=bool: raise ValueError('防串味选项无效')
    for ids in (imgs,mids):
        if not isinstance(ids,list) or not 1<=len(ids)<=8 or any(not isinstance(x,str) for x in ids) or len(set(ids))!=len(ids): raise ValueError('请勾选人物图与通过的预览（各最多 8 个）')
    c=catalog()
    if any(x not in c or c[x]['kind']!='image' for x in imgs): raise ValueError('人物图编号无效')
    controls=resolve_controls(r['owner'],mids)
    d={'id':secrets.token_hex(16),'owner':r['owner'],'created_at':time.time(),'image_ids':imgs,'motion_ids':mids,'motion_preview_ids':mids,'control_kind':[x['kind'] for x in controls],'anti_bleed':anti,'engine':'minimax_h3_ref2va','state':'engine_pending','message':'已排队：H3 引擎安装中，需要带卡；衣服只认人物图，参考视频只提供动作','inference_started':False,'results':[]}
    atomic(GENERATE/(d['id']+'.json'),d)
    return web.json_response(public(d),status=201)

async def generation_result(r):
    try: d=read_owned(GENERATE,r.match_info['jid'],r['owner'])
    except ValueError: raise web.HTTPNotFound()
    slot=r.match_info['slot']
    import re
    if d['state']!='done' or not re.fullmatch(r'result-\d+\.mp4',slot) or slot not in d.get('output_slots',[]): raise web.HTTPNotFound()
    p=HERE/'outputs'/'motion_generate'/d['id']/slot
    if not p.is_file() or p.is_symlink(): raise web.HTTPNotFound()
    return web.FileResponse(p,headers={'Cache-Control':'private, no-store'})

def setup(app):
    for p in (QUEUE,GENERATE,UPLOAD,OUT): p.mkdir(parents=True,exist_ok=True,mode=0o700)
    app.add_routes([web.get('/api/motion-preview/materials',materials),web.get('/api/motion-preview/materials/{mid}/cover',material_cover),web.post('/api/motion-preview/upload',upload),web.post('/api/motion-preview/jobs',submit),web.get('/api/motion-preview/jobs',jobs),web.get('/api/motion-preview/events',events),web.get('/api/motion-preview/jobs/{jid}/results/{slot}',result),web.post('/api/motion-preview/generate',generate),web.get('/api/motion-preview/generation-jobs/{jid}/results/{slot}',generation_result)])
