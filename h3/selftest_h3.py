import sys, json, time, subprocess, os
sys.path.insert(0,'/root/autodl-tmp/studio'); os.chdir('/root/autodl-tmp/studio')
import motion_h3_worker as w
from pathlib import Path
T=Path('/tmp/h3selftest'); T.mkdir(exist_ok=True)
subprocess.run(['ffmpeg','-y','-loglevel','error','-f','lavfi','-i','testsrc=size=320x320:rate=24','-frames:v','1',str(T/'p.png')],check=True)
subprocess.run(['ffmpeg','-y','-loglevel','error','-f','lavfi','-i','testsrc2=size=320x320:rate=24:duration=2','-pix_fmt','yuv420p',str(T/'c.mp4')],check=True)
og=w.graph
def g(*a,**k):
    x=og(*a,**k); x['13']['inputs']['steps']=4; return x
w.graph=g
print('probe',w.Adapter().probe(),flush=True)
d={'id':'0'*31+'1','anti_bleed':True}; p=T/'job.json'
t=time.time()
slot=w.run_one(p,d,0,1,str(T/'p.png'),{'kind':'depth','path':str(T/'c.mp4')},T)
print('OK',slot,round(time.time()-t),'s',json.loads(p.read_text())['message'],flush=True)
