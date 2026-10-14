import json, subprocess, uuid
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from PIL import Image
root=Path('/root/autodl-tmp')
tag='dreamid-selftest-'+uuid.uuid4().hex
vp=root/'videos'/(tag+'.mp4'); ip=root/'inputs'/(tag+'.png')
Image.new('RGB',(64,64),(90,140,180)).save(ip)
with vp.open('xb') as out: out.write(Path('/tmp/dreamid_synthetic_video.mp4').read_bytes())
token=(root/'studio_token.txt').read_text().strip()
def call(method,path,data=None,auth=True):
    headers={'Content-Type':'application/json'}
    if auth:
        headers['X-Studio-Token']='dreamid-isolated-selftest'
        headers['Cookie']='studio_auth='+token+'; studio_session=dreamid-isolated-selftest'
    req=Request('http://127.0.0.1:6006'+path,data=json.dumps(data).encode() if data is not None else None,headers=headers,method=method)
    try:
        with urlopen(req) as r: return r.status,json.loads(r.read())
    except HTTPError as e: return e.code,{}
status,env=call('GET','/api/dreamid/environment');assert status==200 and env['ready'] and not env['gpu_available']
payload={'videos':[vp.name,vp.name],'faces':[{'lib':ip.name}]}
status,d=call('POST','/api/dreamid/jobs',payload);assert status==201 and d['state']=='waiting_gpu' and not d['inference_started'] and d['video_count']==2
assert 'videos' not in d and 'faces' not in d and 'owner' not in d
for bad in [{'videos':['../studio/server.py'],'faces':[{'lib':ip.name}]},{'videos':[],'faces':[]},{'videos':[vp.name],'faces':[{'lib':'../studio/server.py'}]},{'videos':[vp.name],'faces':[{'id':'invalid'}]}]:
    assert call('POST','/api/dreamid/jobs',bad)[0]==400
assert call('GET','/api/dreamid/jobs',auth=False)[0] in (401,403)
status,ls=call('GET','/api/dreamid/jobs');assert any(x['id']==d['id'] for x in ls['jobs'])
assert (root/'studio/dreamid_queue'/(d['id']+'.json')).is_file()
boundary='dreamidSyntheticBoundary'
body=(('--'+boundary+'\r\nContent-Disposition: form-data; name="image"; filename="synthetic.png"\r\nContent-Type: image/png\r\n\r\n').encode()+ip.read_bytes()+('\r\n--'+boundary+'--\r\n').encode())
req=Request('http://127.0.0.1:6006/api/inputs/upload',data=body,headers={'Content-Type':'multipart/form-data; boundary='+boundary,'Cookie':'studio_auth='+token+'; studio_session=dreamid-isolated-selftest','X-Studio-Token':'dreamid-isolated-selftest'},method='POST')
with urlopen(req) as r: uploaded=json.loads(r.read())
status,up=call('POST','/api/dreamid/jobs',{'videos':[vp.name],'faces':[{'lib':uploaded['name']}]})
assert status==201 and up['face_count']==1
print('UPLOAD_SELFTEST_OK: existing upload endpoint and uploaded reference queue')
print('API_SELFTEST_OK: multi-video, path validation, invalid inputs, authentication, private paths omitted, persistent CPU queue; only synthetic media used')
print('TEST_JOB_ID='+d['id'])
print('SYNTHETIC_TAG='+tag)
