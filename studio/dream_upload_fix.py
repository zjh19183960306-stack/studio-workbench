from pathlib import Path
p=Path('/root/autodl-tmp/studio/index.html');s=p.read_text()
old="if(!r.id){const f=new FormData();f.append('image',r.file);const d=await api('/api/upload',{method:'POST',body:f});r.id=d.id}faces.push({id:r.id})"
new="if(r.file&&!r.id){const f=new FormData();f.append('image',r.file,r.file.name);const d=await api('/api/inputs/upload',{method:'POST',body:f});r.lib=d.name;faces.push({lib:d.name});continue}if(!r.id)throw Error('请从素材库勾选人脸照片，或重新上传照片');faces.push({id:r.id})"
assert old in s;s=s.replace(old,new);p.write_text(s);print('UPLOAD_FIX_OK')
