"""Authenticated link downloads; a private persistent aria2 daemon owns transfers."""
import asyncio, contextlib, json, os, re, secrets, shutil, subprocess, time
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from aiohttp import web, ClientSession, ClientTimeout, ClientError

BASE = Path(__file__).resolve().parent
ROOT = BASE / '链接下载'
STATE = BASE / '.link_downloads'
FLOOR = 10 * 1024**3
DOUYIN_FLOOR = 1024**3
YTDLP = '/root/autodl-tmp/bin/yt-dlp'
RPC = 'http://127.0.0.1:16806/jsonrpc'
VIDEO = {'.mp4', '.mov', '.mkv', '.webm', '.m4v'}

def validate(link):
    if not isinstance(link, str) or not 1 <= len(link) <= 8192 or any(ord(c) < 32 and c not in '\r\n\t' for c in link):
        raise ValueError('链接格式无效')
    link = link.strip()
    # Accept phone share text, but only extract an exact Douyin host.
    if not link.startswith(('http://', 'https://', 'magnet:')) or any(c.isspace() for c in link):
        candidates = re.findall(r'https?://[^\s<>]+', link)
        candidates = [x.rstrip('，。；！）)') for x in candidates if is_douyin(x)]
        if len(candidates) != 1: raise ValueError('请粘贴完整链接或含抖音链接的分享文字')
        link = candidates[0]
    u = urlsplit(link)
    if u.scheme == 'magnet':
        xt = parse_qs(u.query).get('xt', [])
        if not any(re.fullmatch(r'urn:btih:(?:[a-fA-F0-9]{40}|[A-Za-z2-7]{32})', x) for x in xt):
            raise ValueError('磁力链接缺少有效 BT 哈希')
    elif u.scheme in ('http', 'https'):
        if not u.hostname or u.username or u.password:
            raise ValueError('请使用完整的 http/https 链接')
    else:
        raise ValueError('只支持磁力、种子链接和 http/https 链接')
    return link

def is_douyin(link):
    u = urlsplit(link)
    return u.scheme in ('http', 'https') and (u.hostname or '').lower() in ('douyin.com', 'www.douyin.com', 'v.douyin.com')

def douyin_error(message):
    message = message.lower()
    if any(x in message for x in ('cookie', 'login', 'sign in', '登录')):
        return '抖音要登录态，请改用视频库上传，或以后再配 cookie'
    if any(x in message for x in ('no space', 'disk full')):
        return '盘快满了，先删视频或等腾空'
    return '抖音下载失败，链接可能失效或平台限制，请改用视频库上传'

def disk_ok(douyin=False):
    if douyin:
        if shutil.disk_usage(ROOT).free < DOUYIN_FLOOR:
            raise ValueError('盘快满了，先删视频或等腾空（抖音下载至少需要剩余 1GB）')
        return
    if shutil.disk_usage(ROOT).free < FLOOR:
        raise ValueError('硬盘剩余不足 10GB，请先腾出空间再开始下载')

class Manager:
    def __init__(self):
        ROOT.mkdir(exist_ok=True)
        STATE.mkdir(mode=0o700, exist_ok=True)
        os.chmod(STATE, 0o700)
        key = STATE/'secret'
        if not key.exists():
            key.write_text(secrets.token_hex(32), encoding='utf-8'); os.chmod(key, 0o600)
        self.secret = key.read_text(encoding='utf-8')
        self.file = STATE/'jobs.json'
        self.jobs = json.loads(self.file.read_text(encoding='utf-8')) if self.file.exists() else {}
        self.lock = asyncio.Lock()
        self.http = None
        self.task = None
        self.online = False
        self.douyin_tasks = {}

    def save(self):
        tmp = self.file.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.jobs, ensure_ascii=False), encoding='utf-8')
        os.chmod(tmp, 0o600); os.replace(tmp, self.file)

    async def rpc(self, method, *params):
        async with self.http.post(RPC, json={'jsonrpc':'2.0', 'id':'studio', 'method':'aria2.'+method,
                                            'params':['token:'+self.secret, *params]}) as r:
            d = await r.json()
        if 'error' in d: raise ValueError('下载引擎：'+str(d['error'].get('message', '请求失败')))
        return d['result']

    async def ensure(self):
        try:
            await self.rpc('getVersion'); self.online = True; return
        except (OSError, ClientError, asyncio.TimeoutError): pass
        session = STATE/'aria2.session'; session.touch(mode=0o600, exist_ok=True)
        config = STATE/'aria2.conf'
        options = {'enable-rpc':'true', 'rpc-listen-all':'false', 'rpc-listen-port':'16806',
                   'rpc-secret':self.secret, 'dir':str(ROOT), 'input-file':str(session),
                   'save-session':str(session), 'save-session-interval':'5',
                   'continue':'true', 'file-allocation':'none', 'max-concurrent-downloads':'3',
                   'max-download-result':'1000', 'keep-unfinished-download-result':'true',
                   'force-save':'false', 'seed-time':'0', 'bt-save-metadata':'true',
                   'follow-torrent':'true', 'auto-file-renaming':'false', 'allow-overwrite':'false',
                   'check-certificate':'true', 'no-netrc':'true', 'follow-metalink':'false',
                   'enable-dht':'true', 'enable-dht6':'false',
                   'dht-file-path':str(STATE/'dht.dat'), 'log':str(STATE/'aria2.log'), 'log-level':'warn'}
        # Keep administrator BT tuning when the download engine is relaunched.
        bt_keys = {'bt-tracker', 'enable-dht', 'enable-dht6', 'enable-peer-exchange',
                   'bt-enable-lpd', 'dht-file-path', 'dht-file-path6'}
        if config.exists():
            for line in config.read_text(encoding='utf-8').splitlines():
                key, sep, value = line.partition('=')
                if sep and key in bt_keys:
                    options[key] = value
        config.write_text(''.join(k+'='+v+'\n' for k,v in options.items()), encoding='utf-8')
        os.chmod(config, 0o600)
        with open(STATE/'daemon.log', 'ab') as log:
            subprocess.Popen(['aria2c', '--conf-path='+str(config)], stdin=subprocess.DEVNULL,
                             stdout=log, stderr=log, start_new_session=True)
        for _ in range(30):
            await asyncio.sleep(.2)
            try:
                await self.rpc('getVersion'); self.online = True; return
            except (OSError, ClientError, asyncio.TimeoutError): pass
        raise ValueError('aria2 未能启动，请检查下载引擎日志')

    async def submit(self, link):
        link = validate(link)
        async with self.lock:
            douyin = is_douyin(link)
            disk_ok(douyin)
            if not douyin: await self.ensure()
            if sum(j['status'] not in ('complete','cancelled','error') for j in self.jobs.values()) >= 50:
                raise ValueError('未完成任务已达 50 个，请先完成或取消')
            ident = secrets.token_hex(8); gid = secrets.token_hex(8)
            folder = ROOT/ident; folder.mkdir()
            job = {'id':ident, 'gid':gid, 'link':link, 'created':int(time.time()), 'status':'waiting',
                   'total':0, 'completed':0, 'speed':0, 'files':[], 'videos':[], 'error':'', 'paused':False, 'engine':'douyin' if douyin else 'aria2'}
            # Persist intent before invoking RPC. Recover it if the app exits between these operations.
            self.jobs[ident] = job; self.save()
            if douyin: return ident
            try:
                await self.rpc('addUri', [link], {'gid':gid, 'dir':str(folder), 'pause':'false', 'force-save':'false'})
                await self.rpc('saveSession')
            except Exception:
                job.update(status='error', error='提交失败，可以点重试'); self.save(); raise
            return ident

    def publish(self, job, files):
        folder = (ROOT/job['id']).resolve()
        entries = []; vids = []
        for f in files:
            p = Path(f['path'])
            if f.get('selected') != 'true' or int(f.get('completedLength',0)) != int(f.get('length',0)):
                continue
            if not p.is_file() or not p.resolve().is_relative_to(folder): continue
            rel = str(p.relative_to(folder)); entries.append(rel)
            if p.suffix.lower() in VIDEO:
                name = 'link_'+job['id']+'_'+str(len(vids)+1)+p.suffix.lower()
                dest = BASE.parent/'videos'/name
                dest.parent.mkdir(exist_ok=True)
                if not dest.exists() and not dest.is_symlink(): dest.symlink_to(p)
                if dest.is_symlink() and dest.resolve() == p.resolve(): vids.append(name)
        job.update(files=entries, videos=vids)

    async def refresh(self):
        async with self.lock:
            await self.refresh_douyin()
            await self.ensure()
            low = shutil.disk_usage(ROOT).free < FLOOR
            # aria2 saves a metadata parent's GID, then assigns a new child GID
            # after restart. Reconnect by our isolated task directory, not by name.
            keys = ['gid','dir','status','followedBy']
            live = await self.rpc('tellActive', keys)
            live += await self.rpc('tellWaiting', 0, 1000, keys)
            by_dir = {}
            for item in live: by_dir.setdefault(item['dir'], []).append(item)
            for job in self.jobs.values():
                if job.get('engine') == 'douyin': continue
                matches = by_dir.get(str(ROOT/job['id']), [])
                if job['status'] == 'cancelled':
                    for item in matches:
                        with contextlib.suppress(ValueError): await self.rpc('forceRemove', item['gid'])
                        with contextlib.suppress(ValueError): await self.rpc('removeDownloadResult', item['gid'])
                    with contextlib.suppress(ValueError): await self.rpc('removeDownloadResult', job['gid'])
                    continue
                if job['status'] == 'complete': continue
                try:
                    d = await self.rpc('tellStatus', job['gid'])
                except ValueError:
                    if matches:
                        job['gid'] = matches[-1]['gid']
                        d = await self.rpc('tellStatus', job['gid'])
                    else:
                        if job['status'] == 'error': continue
                        # Rebuild intent if the last aria2 session checkpoint missed it.
                        await self.rpc('addUri', [job['link']], {'gid':job['gid'], 'dir':str(ROOT/job['id']), 'force-save':'false',
                                                              'pause':'true' if job['paused'] or low else 'false'})
                        continue
                seen = set()
                while d.get('followedBy'):
                    gid = d['followedBy'][0]
                    if gid in seen: break
                    seen.add(gid); job['gid'] = gid
                    d = await self.rpc('tellStatus', gid)
                status = d['status']
                if status == 'removed': status = 'cancelled'
                if status in ('active','waiting') and (low or job['paused']):
                    await self.rpc('forcePause', job['gid']); status = 'paused'
                    job['paused'] = True
                    if low: job['error'] = '剩余不足 10GB，已自动暂停；腾出空间后点继续'
                # aria2 reloads a paused session lazily: keep the last measured progress
                # until it initializes the control file on unpause.
                unloaded = status == 'paused' and int(d.get('totalLength',0)) == 0
                job.update(status=status, total=job['total'] if unloaded else int(d.get('totalLength',0)),
                           completed=job['completed'] if unloaded else int(d.get('completedLength',0)),
                           speed=0 if status == 'paused' else int(d.get('downloadSpeed',0)))
                if status == 'error': job['error'] = 'aria2 '+d.get('errorCode','')+'：'+d.get('errorMessage','下载失败')
                if status == 'complete':
                    self.publish(job, d.get('files',[])); job['error'] = ''; job['speed'] = 0
            self.save()

    async def action(self, ident, action):
        await self.refresh()  # Resolve a torrent/metadata child before controlling the transfer.
        async with self.lock:
            if ident not in self.jobs: raise ValueError('任务不存在')
            j = self.jobs[ident]
            if j.get('engine') == 'douyin':
                if j['status'] in ('complete','cancelled'): raise ValueError('任务已结束')
                if action not in ('pause','resume','cancel'): raise ValueError('操作无效')
                if action == 'resume': disk_ok(True)
                j.update(status={'pause':'paused','resume':'waiting','cancel':'cancelled'}[action],
                         paused=action != 'resume', speed=0, error='')
                task = self.douyin_tasks.get(ident)
                if task and action != 'resume': task.cancel()
                self.save(); return
            await self.ensure()
            if j['status'] in ('complete','cancelled'): raise ValueError('任务已结束')
            if action == 'pause':
                j['paused'] = True; self.save()
                await self.rpc('forcePause', j['gid']); j.update(status='paused',speed=0)
            elif action == 'resume':
                disk_ok()
                if j['status'] == 'error':
                    with contextlib.suppress(ValueError): await self.rpc('removeDownloadResult', j['gid'])
                    gid = secrets.token_hex(8)
                    await self.rpc('addUri', [j['link']], {'gid':gid, 'dir':str(ROOT/ident), 'force-save':'false'})
                    j['gid'] = gid
                else: await self.rpc('unpause', j['gid'])
                j.update(paused=False,status='waiting',error='')
            elif action == 'cancel':
                j.update(status='cancelled',paused=True,speed=0); self.save()
                with contextlib.suppress(ValueError): await self.rpc('forceRemove', j['gid'])
                with contextlib.suppress(ValueError): await self.rpc('removeDownloadResult', j['gid'])
            else: raise ValueError('操作无效')
            self.save(); await self.rpc('saveSession')

    async def refresh_douyin(self):
        for ident, task in list(self.douyin_tasks.items()):
            if task.done():
                with contextlib.suppress(asyncio.CancelledError, Exception): task.result()
                del self.douyin_tasks[ident]
        for j in self.jobs.values():
            if j.get('engine') != 'douyin': continue
            if j['status'] == 'active' and j['id'] not in self.douyin_tasks:
                j.update(status='waiting', speed=0)
            if j['status'] == 'waiting' and not j['paused'] and not self.douyin_tasks:
                self.douyin_tasks[j['id']] = asyncio.create_task(self.run_douyin(j))
                break

    async def run_douyin(self, j):
        proc = None
        try:
            disk_ok(True)
            j.update(status='active', error='', speed=0); self.save()
            folder = ROOT/j['id']
            proc = await asyncio.create_subprocess_exec(
                YTDLP, '--ignore-config', '--no-playlist', '--no-colors', '--newline',
                '--socket-timeout', '20', '--retries', '2', '--fragment-retries', '2',
                '--max-filesize', '700M', '-f', 'best[ext=mp4]/best',
                '-o', str(folder/'douyin.%(ext)s'),
                '--progress-template', 'download:STUDIO:%(progress)j', '--', j['link'],
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            errors = ''; deadline = time.monotonic() + 1800
            reader = asyncio.create_task(proc.stdout.readline())
            try:
                while True:
                    disk_ok(True)
                    if time.monotonic() > deadline: raise ValueError('抖音下载超时，请改用视频库上传')
                    done, _ = await asyncio.wait([reader], timeout=1)
                    if not done: continue
                    line = reader.result()
                    if not line: break
                    text = line.decode('utf-8', 'replace').strip()
                    if text.startswith('STUDIO:'):
                        with contextlib.suppress(ValueError, TypeError):
                            d = json.loads(text[7:])
                            j.update(completed=int(d.get('downloaded_bytes') or 0),
                                     total=int(d.get('total_bytes') or d.get('total_bytes_estimate') or 0),
                                     speed=int(d.get('speed') or 0))
                            self.save()
                    else: errors = (errors + ' ' + text)[-12000:]
                    reader = asyncio.create_task(proc.stdout.readline())
            finally:
                reader.cancel()
                with contextlib.suppress(asyncio.CancelledError): await reader
            code = await proc.wait()
            if code: raise ValueError(douyin_error(errors))
            files = [{'path':str(p), 'selected':'true', 'length':p.stat().st_size,
                      'completedLength':p.stat().st_size} for p in folder.iterdir()
                     if p.is_file() and p.suffix.lower() in VIDEO]
            if not files: raise ValueError('未取得可用视频，请改用视频库上传')
            self.publish(j, files)
            size = sum(f['length'] for f in files)
            j.update(status='complete', total=size, completed=size, speed=0, error='')
        except asyncio.CancelledError:
            if j['status'] == 'active': j.update(status='waiting', speed=0)
            raise
        except Exception as e:
            message = str(e)
            if not message.startswith(('抖音', '盘快满', '未取得')): message = douyin_error(message)
            j.update(status='error', error=message, speed=0)
        finally:
            if proc and proc.returncode is None:
                with contextlib.suppress(ProcessLookupError): proc.terminate()
                try: await asyncio.wait_for(proc.wait(), 5)
                except asyncio.TimeoutError:
                    with contextlib.suppress(ProcessLookupError): proc.kill()
                    await proc.wait()
            self.save()

    async def loop(self):
        while True:
            try: await self.refresh(); self.online = True
            except (OSError, ClientError, ValueError, asyncio.TimeoutError): self.online = False
            await asyncio.sleep(2)

    async def lifetime(self, app):
        self.http = ClientSession(timeout=ClientTimeout(total=5))
        try: await self.ensure()
        except (OSError, ClientError, ValueError, asyncio.TimeoutError): self.online = False
        self.task = asyncio.create_task(self.loop())
        yield
        self.task.cancel()
        with contextlib.suppress(asyncio.CancelledError): await self.task
        with contextlib.suppress(Exception): await self.rpc('saveSession')
        for task in self.douyin_tasks.values(): task.cancel()
        await asyncio.gather(*self.douyin_tasks.values(), return_exceptions=True)
        self.save(); await self.http.close()

def require_login(request):
    import server
    if not secrets.compare_digest(request.cookies.get('studio_auth_v2',''), server.ACCESS_TOKEN):
        raise web.HTTPForbidden(text='需要登录')

async def listing(request):
    require_login(request); m = request.app['link_downloads']
    async with m.lock:
        jobs = [{k:v for k,v in j.items() if k not in ('link','gid','paused')} for j in m.jobs.values()]
    return web.json_response({'jobs':jobs, 'free':shutil.disk_usage(ROOT).free, 'online':m.online})

async def add(request):
    require_login(request)
    ident = await request.app['link_downloads'].submit((await request.json()).get('link'))
    return web.json_response({'id':ident})

async def act(request):
    require_login(request)
    await request.app['link_downloads'].action(request.match_info['id'], (await request.json()).get('action'))
    return web.json_response({'ok':True})

def setup(app):
    m = Manager(); app['link_downloads'] = m; app.cleanup_ctx.append(m.lifetime)
    app.add_routes([web.get('/api/link-downloads', listing), web.post('/api/link-downloads', add),
                    web.post('/api/link-downloads/{id}', act)])
