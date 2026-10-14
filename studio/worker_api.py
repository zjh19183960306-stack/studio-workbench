"""Owner-protected streaming queue summaries and generated result downloads."""
import asyncio, json, re
from pathlib import Path
from aiohttp import web
from worker_common import ID, summary

HERE=Path(__file__).resolve().parent

def listing(queue,owner):
    items=[]
    for p in queue.glob('*.json'):
        if not ID.fullmatch(p.stem): continue
        try:
            d=json.loads(p.read_text(encoding='utf-8'))
            if d.get('owner')==owner: items.append(summary(d))
        except (OSError,ValueError): pass
    return sorted(items,key=lambda x:x.get('created_at',0),reverse=True)[:100]

def setup(app,queue,prefix):
    async def events(request):
        response=web.StreamResponse(headers={'Content-Type':'text/event-stream','Cache-Control':'no-cache',
                                             'X-Accel-Buffering':'no'})
        await response.prepare(request)
        try:
            for _ in range(150):
                d={'jobs':listing(queue,request['owner'])}
                await response.write(('data: '+json.dumps(d,ensure_ascii=False)+'\n\n').encode())
                await asyncio.sleep(2)
        except (ConnectionResetError, asyncio.CancelledError): pass
        return response
    async def result(request):
        jid=request.match_info['jid']; slot=request.match_info['slot']
        if not ID.fullmatch(jid) or not re.fullmatch(r'result-\d+\.(mp4|json)',slot): raise web.HTTPNotFound()
        try: d=json.loads((queue/(jid+'.json')).read_text(encoding='utf-8'))
        except (OSError,ValueError): raise web.HTTPNotFound()
        if d.get('owner')!=request['owner'] or d.get('state')!='done': raise web.HTTPNotFound()
        url=prefix+'/jobs/'+jid+'/results/'+slot
        if url not in d.get('results',[]): raise web.HTTPNotFound()
        root=HERE.parent/'outputs'/'faceswap'
        p=(root/jid/slot).resolve()
        if not p.is_relative_to(root.resolve()) or not p.is_file(): raise web.HTTPNotFound()
        headers={'Cache-Control':'private, no-store'}
        if request.query.get('download')=='1': headers['Content-Disposition']='attachment; filename="'+slot+'"'
        return web.FileResponse(p,headers=headers)
    app.add_routes([web.get(prefix+'/events',events),web.get(prefix+'/jobs/{jid}/results/{slot}',result)])
