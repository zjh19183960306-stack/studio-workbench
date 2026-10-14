import asyncio,tempfile,json
from pathlib import Path
from aiohttp import web
from aiohttp.test_utils import TestClient,TestServer
from PIL import Image
import server,pose,library
async def run():
    with tempfile.TemporaryDirectory(prefix='auth-isolated-') as d:
        root=Path(d);library.R=root;pose.POSE=root/'pose';pose.LIB=pose.POSE/'library.json';pose.POSE.mkdir();(root/'outputs').mkdir()
        for p in [root/'outputs'/'test.png',pose.POSE/'test_body.png',pose.POSE/'test_face.png']:Image.new('RGB',(32,32),(10,20,30)).save(p)
        pose.LIB.write_text(json.dumps({'items':[{'id':'test','tok':'test'}]}))
        app=web.Application(middlewares=[server.guard]);app.on_response_prepare.append(server.privacy_headers)
        app.update(owners={'tester'},jobs={'test':{'owner':'tester','images':[{'filename':'test.png'}]}},poses={'test':{'owner':'tester','tok':'test'}})
        app.add_routes([web.get('/api/jobs/{job}/images/{index}',server.output),web.get('/api/poses/{lid}/thumb',pose.lib_thumb),web.get('/api/pose/{key}/skeleton',pose.skeleton),web.post('/api/logout',server.studio_logout)])
        async with TestClient(TestServer(app)) as client:
            headers={'Cookie':'studio_auth='+server.ACCESS_TOKEN+'; studio_session=tester'}
            paths=['/api/jobs/test/images/0','/api/jobs/test/images/0?chunk=0&size=16','/api/poses/test/thumb','/api/pose/test/skeleton?face=0','/api/pose/test/skeleton?face=1']
            for path in paths:
                for auth,status in [(False,403),(True,200)]:
                    r=await client.get(path,headers=headers if auth else {});assert r.status==status;assert 'no-store' in r.headers['Cache-Control'];await r.read()
            r=await client.post('/api/logout',headers=headers);assert r.status==200
            assert r.cookies['studio_auth']['max-age']=='0' and r.cookies['studio_session']['max-age']=='0'
            print('isolated synthetic jobs/pose/skeleton cases=5 anonymous=403 authenticated=200 no-store=PASS; logout both cookies cleared=PASS')
asyncio.run(run())
