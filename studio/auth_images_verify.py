import sys,json,secrets,subprocess,tempfile,re,time
from pathlib import Path
sys.path.insert(0,'/root/autodl-tmp/studio')
from PIL import Image
import server,library,pack,video,thumbs,lbcache,download_links
R=Path('/root/autodl-tmp'); tag='authcheck_'+secrets.token_hex(8); name=tag+'.png'; created=[]; cache=[]
def curl(base,path,jar=None,method='GET',data=None,headers=()):
    args=['curl','-sS','--max-time','30','-X',method,'-D',str(tmp/'headers'),'-o',str(tmp/'body'),'-w','%{http_code}']
    if method=='HEAD': args+=['--head']
    if jar: args+=['-b',str(jar),'-c',str(jar)]
    for h in headers:args+=['-H',h]
    if data is not None:
        (tmp/'request').write_text(json.dumps(data));args+=['-H','Content-Type: application/json','--data-binary','@'+str(tmp/'request')]
    p=subprocess.run(args+[base+path],capture_output=True,text=True)
    assert p.returncode==0,'curl transport failure'
    code=int(p.stdout);hs=(tmp/'headers').read_text().lower();body=(tmp/'body').read_bytes()
    assert 'cache-control: private, no-store' in hs,'missing no-store'
    return code,hs,body
with tempfile.TemporaryDirectory(prefix='studio-authcheck-') as td:
    tmp=Path(td)
    try:
        for p in [pack.INPUTS/name,library.HIST/name]:
            p.parent.mkdir(parents=True,exist_ok=True);assert not p.exists();Image.new('RGB',(64,64),(48,96,160)).save(p);created.append(p)
        b=time.strftime('%Y%m%d-%H%M%S');bd=pack.OUTPUTS/b;assert not bd.exists();bd.mkdir();created.append(bd)
        for p in [bd/name,bd/'_rejected'/name]:
            p.parent.mkdir(exist_ok=True);Image.new('RGB',(64,64),(48,96,160)).save(p);created.append(p)
        vp=video.VIDEOS/(tag+'.mp4');vp.parent.mkdir(exist_ok=True);assert not vp.exists();vp.write_bytes(b'synthetic-test-file');created.append(vp)
        cover=video._cover_path(vp);cover.parent.mkdir(exist_ok=True);Image.new('RGB',(64,64),(48,96,160)).save(cover);created.append(cover)
        k=lbcache._safe_key(lbcache.src_token(library.HIST/name))
        for dest in [lbcache.PREV/(k+'.jpg'),lbcache.VIEW/(k+'.jpg')]:
            dest.parent.mkdir(parents=True,exist_ok=True);Image.new('RGB',(64,64),(48,96,160)).save(dest);cache.append(dest)
        cache.append(lbcache.META/(k+'.json'))
        cache.extend([thumbs.thumb_path('inputs',name),thumbs.thumb_path('history',name),thumbs.thumb_path('batch-'+b,name)])
        signed=download_links.mint(library.HIST/name)['url'];from urllib.parse import urlsplit,quote
        dl=urlsplit(signed);dlpath=dl.path+'?'+dl.query;created.append(download_links.STORE/(dl.path.rsplit('/',1)[1]+'.json'))
        hist='/api/history/file/'+name
        paths=[('input','/api/inputs/'+name),('input thumb','/api/inputs/'+name+'?thumb=1'),('history',hist),('history thumb',hist+'?thumb=1'),('batch','/api/batches/'+b+'/files/'+name),('batch thumb','/api/batches/'+b+'/files/'+name+'?thumb=1'),('rejected','/api/batches/'+b+'/rejected/'+name),('cover','/api/videos/'+vp.name+'/cover'),('lightbox preview','/api/lb/file?url='+quote(hist,safe='')+'&kind=preview'),('lightbox view','/api/lb/file?url='+quote(hist,safe='')+'&kind=view'),('lightbox full chunk','/api/lb/file?url='+quote(hist,safe='')+'&kind=full&chunk=0&size=32'),('signed download',dlpath)]
        for base,label in [('http://127.0.0.1:6006','loopback'),('https://bbstudio6006.serveousercontent.com','public')]:
            jar=tmp/(label+'.cookies');jar.touch()
            c,h,body=curl(base,'/');assert c==200 and b'<img' not in body.lower() and b'<video' not in body.lower();print(label+' home/login-page 200 no-images PASS')
            assert curl(base,'/api/login',jar,'POST',{'password':'incorrect-'+tag})[0]==403
            assert curl(base,'/api/login',jar,'POST',{'password':server.ACCESS_TOKEN})[0]==200
            for title,path in paths:
                c,h,body=curl(base,path);assert c==403 and 'image/' not in h
                c,h,body=curl(base,path,jar);assert c==200 and len(body)>0,(title,c)
                assert 'image/' in h or title=='signed download'
                print(label+' '+title+' anonymous=403 authenticated=200 no-store=PASS')
            # Header/query tokens cannot replace cookie on protected routes.
            assert curl(base,hist+'?token='+server.ACCESS_TOKEN)[0]==403
            assert curl(base,hist,headers=['X-Studio-Token: '+server.ACCESS_TOKEN])[0]==403
            assert curl(base,hist,headers=['Cookie: studio_auth=invalid'])[0]==403
            assert curl(base,hist,jar,headers=['Range: bytes=0-15'])[0]==206
            assert curl(base,hist,headers=['Range: bytes=0-15'])[0]==403
            c,h,body=curl(base,'/api/logout',jar,'POST');assert c==200 and 'clear-site-data: "cache"' in h
            for title,path in paths:assert curl(base,path,jar)[0]==403
            print(label+' wrong-password/token-only/invalid-cookie/range/logout '+str(len(paths))+' protected URLs PASS')
        app=server.create_app();count=0
        for route in app.router.routes():
            if route.method not in ('GET','HEAD'):continue
            path=re.sub(r'\{[^}]+\}',tag,route.resource.canonical)
            if path=='/':continue
            c,h,body=curl('http://127.0.0.1:6006',path,method=route.method);assert c==403,(route.method,c)
            count+=1
        print('registered GET/HEAD routes checked='+str(count)+' all anonymous=403 PASS')
        # Only known synthetic cache keys are cleaned.
        for p in [pack.INPUTS/name,library.HIST/name,bd/name,bd/'_rejected'/name]:
            k=lbcache._safe_key(lbcache.src_token(p))
            cache.extend([lbcache.META/(k+'.json'),lbcache.PREV/(k+'.jpg'),lbcache.VIEW/(k+'.jpg')])
        cache.extend([thumbs.thumb_path('inputs',name),thumbs.thumb_path('history',name),thumbs.thumb_path('batch-'+b,name)])
        print('RESULT PASS synthetic image endpoints='+str(len(paths))+'; user media read=0')
    finally:
        for p in cache+list(reversed(created)):
            if p.is_file():p.unlink()
        rd=bd/'_rejected' if 'bd' in locals() else None
        if rd and rd.is_dir():rd.rmdir()
        if 'bd' in locals() and bd.is_dir():bd.rmdir()
