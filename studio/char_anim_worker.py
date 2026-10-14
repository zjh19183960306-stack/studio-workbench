import fcntl,json,os,time,threading,re,shutil
from pathlib import Path
import importlib.util
_spec=importlib.util.spec_from_file_location('char_anim_transport',Path(__file__).resolve().parent/'faceswap_worker.py')
transport=importlib.util.module_from_spec(_spec);_spec.loader.exec_module(transport)
from worker_common import atomic,ID
HERE=Path(__file__).resolve().parent;Q=HERE/'char_anim_queue';STATUS=HERE/'char_anim_worker_status.json'
transport.PREFIX='char-anim-worker'
def tick():
    probe=transport.rpc('probe')
    for p in Q.glob('*.json'):
        if not ID.fullmatch(p.stem):continue
        d=json.loads(p.read_text(encoding='utf-8'))
        if d['state'] in ('done','failed'):continue
        try:
            if not probe.get('gpu'):
                if d['state'] not in ('running','collecting'):
                    d.update(state='waiting_gpu',message='已排队，需要带卡开机',inference_started=False);atomic(p,d)
                continue
            if d['state'] in ('waiting_gpu','queued'):
                if not probe.get('idle'):continue
                if not probe.get('ready'):
                    d.update(state='failed',message='本地动画引擎权重或依赖未齐，请查看安装报告');atomic(p,d);continue
                # Only after real GPU AND readiness gate resolve selected inputs.
                cat=json.loads((HERE/'char_anim_catalog.json').read_text(encoding='utf-8'))
                imgs=[transport.allowed_file(cat[x]['path'],'face') for x in d['image_ids']]
                vid=transport.allowed_file(cat[d['video_id']]['path'],'video')
                slots=['f'+str(i)+x.suffix.lower() for i,x in enumerate(imgs)]
                vs='v0'+vid.suffix.lower();spec={'id':d['id'],'images':slots,'video':vs,'anti_bleed':d['anti_bleed']}
                r=transport.rpc('init',d['id'],payload=spec)
                if not r.get('gpu'):continue
                d.update(state='staging',message='正在传送所选编号素材',step=1);atomic(p,d)
                for slot,src in zip(slots+[vs],imgs+[vid]):transport.rpc('upload',d['id'],slot,source=src,timeout=1800)
                r=transport.rpc('start',d['id']);d.update(state=r['state'],message=r.get('message','引擎准备中'),step=2);atomic(p,d)
            r=transport.rpc('status',d['id'])
            if r['state']=='done':
                out=HERE.parent/'outputs'/'char_anim'/d['id'];out.mkdir(parents=True,exist_ok=True,mode=0o700)
                slots=r['outputs'];urls=[]
                for slot in slots:
                    if not re.fullmatch(r'result-\d+\.mp4',slot):raise ValueError()
                    if shutil.disk_usage(out).free<r['output_sizes'][slot]+512*1024**2:raise ValueError()
                    temp=out/(slot+'.part');transport.rpc('download',d['id'],slot,dest=temp,timeout=1800);temp.replace(out/slot)
                    urls.append('/api/char-anim/jobs/'+d['id']+'/results/'+slot)
                d.update(state='done',message='角色动画完成',results=urls,output_slots=slots,progress=100,step=5)
            elif r['state']=='missing':d.update(state='failed',message='远端任务记录缺失，请重新提交')
            else:d.update({k:r[k] for k in ('state','message','step','progress','inference_started') if k in r})
            atomic(p,d)
            break
        except transport.TransportError:
            d.update(message='引擎机连接暂时中断，正在重连');atomic(p,d)
        except Exception:
            d.update(state='failed',message='动画执行失败，请检查本地引擎依赖、权重和磁盘');atomic(p,d)
def main():
    os.umask(0o077);Q.mkdir(exist_ok=True);lock=open(HERE/'char_anim_worker.lock','a')
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:return
    def pulse():
        while True:atomic(STATUS,{'pid':os.getpid(),'heartbeat':time.time(),'automatic_worker':True});time.sleep(5)
    threading.Thread(target=pulse,daemon=True).start()
    while True:
        try:tick()
        except Exception:pass
        time.sleep(5)
if __name__=='__main__':main()
