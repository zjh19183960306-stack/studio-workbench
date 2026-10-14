from pathlib import Path
import time, json
p=Path('/root/autodl-tmp/studio')
stamp=str(int(time.time()))
for n in ['server.py','index.html']:
    (p/(n+'.before-dreamid-'+stamp)).write_bytes((p/n).read_bytes())
s=(p/'server.py').read_text()
if 'import dreamid; dreamid.setup(app)' not in s:
    s=s.replace('    import video; video.setup(app)','    import video; video.setup(app)\n    import dreamid; dreamid.setup(app)')
(p/'server.py').write_text(s)
s=(p/'index.html').read_text()
if 'id="dreamStart"' not in s:
    s=s.replace('<div id="vlRows"></div>','<div id="vlRows"></div><div style="margin-top:16px"><b>DreamID-V 换脸</b><p>先在视频库勾选视频，再从素材库勾选人脸照片，或用现有「添加参考照片」上传。多张照片会作为同一人的参考。</p><button id="dreamStart">开始 DreamID-V</button><button id="dreamRefresh">查看排队</button><p id="dreamMsg" role="status">程序已接好，真正换脸需要带卡开机。当前只校验路径并排队。</p></div>')
    js='''
async function dreamRefresh(){try{const d=await api('/api/dreamid/jobs');$('dreamMsg').textContent=d.jobs.length?d.jobs.map(j=>'视频 '+j.video_count+' 个，参考照片 '+j.face_count+' 张：'+j.message).join('\\n'):'还没有排队任务。程序已接好，真正换脸需要带卡开机。'}catch(e){$('dreamMsg').textContent=e.message}}
$('dreamRefresh').onclick=dreamRefresh;
$('dreamStart').onclick=async()=>{const b=$('dreamStart');if(!vlSelected.size||!references.length){$('dreamMsg').textContent='请先勾选视频，再添加或勾选人脸参考照片';return}b.disabled=true;try{$('dreamMsg').textContent='第 1 步：收集所选视频和照片…';const faces=[];for(const r of references){if(r.lib){faces.push({lib:r.lib});continue}if(!r.id){const f=new FormData();f.append('image',r.file);const d=await api('/api/upload',{method:'POST',body:f});r.id=d.id}faces.push({id:r.id})}$('dreamMsg').textContent='第 2 步：校验路径和检查环境…';const d=await api('/api/dreamid/jobs',json({videos:[...vlSelected],faces}));$('dreamMsg').textContent='第 3 步：已保存排队任务（'+d.video_count+' 个视频）。'+d.message+(d.environment.stale?' 环境记录较早，带卡前需要重新检查。':'')}catch(e){$('dreamMsg').textContent=e.message}finally{b.disabled=false}};
'''
    s=s.replace("const vlSelected=new Set();","const vlSelected=new Set();"+js)
(p/'index.html').write_text(s)
print('PATCH_OK')
