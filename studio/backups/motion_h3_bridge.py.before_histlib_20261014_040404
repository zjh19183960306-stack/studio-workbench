"""Opt-in MODULE A hook. Caller owns GPU scheduling; no old worker changes."""
import motion_preview as m
from worker_common import atomic

def dispatch_one(adapter, job_id=None):
    """adapter.probe() and adapter.enqueue(manifest) must be local H3 only.

    Only a verified motion-control capability allows dispatch. No original
    reference-video path is ever part of the H3 manifest.
    """
    probe=adapter.probe()
    if not (probe.get('gpu') and probe.get('ready') and probe.get('idle')
            and probe.get('control_video_input') is True
            and probe.get('engine')=='minimax_h3_ref2va_local'):
        return {'state':'engine_pending','message':'H3 引擎安装中，需要带卡'}
    for p in sorted(m.GENERATE.glob('*.json')):
        if job_id is not None and p.stem!=job_id: continue
        try:
            import json
            d=json.loads(p.read_text(encoding='utf-8'))
            if d['state']!='engine_pending': continue
            controls=m.resolve_controls(d['owner'],d['motion_ids'])
            c=m.catalog()
            images=[{'id':i,'path':c[i]['path']} for i in d['image_ids'] if c[i]['kind']=='image']
            if len(images)!=len(d['image_ids']): continue
            manifest={'id':d['id'],'owner':d['owner'],'engine':'minimax_h3_ref2va_local',
                      'images':images,'controls':controls,'anti_bleed':d['anti_bleed'],
                      'appearance_source':'images_only','motion_source':'approved_controls_only'}
            # Adapter must durably/idempotently enqueue under this id, not start a
            # second inference loop. Never feed these controls into RGB extraction.
            reply=adapter.enqueue(manifest)
            if reply.get('accepted') is True:
                d.update(state='queued',message='H3 已接收，等待显卡空闲',inference_started=False)
                atomic(p,d)
                return {'id':d['id'],'state':'queued'}
        except (OSError,ValueError,KeyError,TypeError): continue
    return {'state':'engine_pending','message':'等待可接入的出片任务'}
