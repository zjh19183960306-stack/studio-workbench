"""Loopback-only local image studio; run only after its backend is installed."""
from __future__ import annotations
import asyncio, copy, hashlib, json, re, secrets, time
from pathlib import Path, PurePosixPath
from urllib.parse import urlencode
from aiohttp import ClientError, ClientSession, ClientTimeout, WSMsgType, web

HERE = Path(__file__).resolve().parent
BACKEND = 'http://127.0.0.1:8192'
TEMPLATE = HERE / 'workflow.json'
ACCESS_TOKEN = Path('/root/autodl-tmp/studio_token.txt').read_text().strip()
import os
NO_TOKEN = False  # Authentication is mandatory, including signed downloads.

def _https(request):
    xf = (request.headers.get('X-Forwarded-Proto') or '').split(',')[0].strip().lower()
    return request.secure or xf == 'https' or request.url.scheme == 'https'

def set_auth_cookie(resp, request):
    # Session cookie only: closing the browser requires password again.
    # Cookie name studio_auth_v2 invalidates any old 30-day studio_auth cookies.
    resp.set_cookie('studio_auth_v2', ACCESS_TOKEN, httponly=True, samesite='Lax',
                    path='/', secure=_https(request))
    # Clear legacy long-lived cookie if present.
    resp.del_cookie('studio_auth', path='/')
    return resp

LOGIN_PAGE = '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>工作台登录</title><style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#121712;color:#e8eee9;font-family:system-ui,sans-serif}.box{width:min(360px,92vw);padding:28px 24px;border:1px solid #303b33;border-radius:14px;background:#1a211c}h1{font-size:18px;margin:0 0 8px}p{margin:0 0 18px;color:#9aab9f;font-size:13px}input{width:100%;box-sizing:border-box;padding:12px;border-radius:8px;border:1px solid #3a463e;background:#0f140f;color:#e8eee9;font-size:16px}button{margin-top:12px;width:100%;padding:12px;border:0;border-radius:8px;background:#9dccad;color:#102015;font-weight:600;font-size:15px;cursor:pointer}.err{color:#efa79b;font-size:13px;min-height:18px;margin-top:10px}.hint{margin-top:14px;font-size:12px;color:#7f9086}</style></head><body><div class="box"><h1>云端工作台</h1><p>请输入访问密码</p><form id="f" method="post" action="/login"><input id="pw" name="password" type="password" autocomplete="current-password" placeholder="密码" autofocus required><button type="submit" id="go">进入</button></form><div class="err" id="err"></div><div class="hint">备选：地址后加 /?token=密码</div></div><script>(function(){var f=document.getElementById("f"),pw=document.getElementById("pw"),err=document.getElementById("err");if(!f)return;f.addEventListener("submit",function(ev){ev.preventDefault();err.textContent="";fetch("/api/login",{method:"POST",headers:{"Content-Type":"application/json"},credentials:"same-origin",body:JSON.stringify({password:pw.value})}).then(function(r){return r.json().catch(function(){return {};}).then(function(d){if(!r.ok){err.textContent=d.error||("登录失败 "+r.status);return;}location.replace("/");});}).catch(function(){err.textContent="网络错误，可直接点进入用表单提交";});});})();</script></body></html>'
# 默认锁定规则（物品 / 角色两种）；custom=True（自定义提示词）时服务端不追加任何内容
LOCKS = {
 'item': ('【硬性要求】严格保持参考照片中物品的外形、颜色、材质、纹理、logo、配件和所有细节完全不变；'
          '只改变拍摄角度和镜头远近；不添加参考照片里没有的任何东西。',
          '改变外形，改变颜色，改变材质，改变纹理，改变logo，额外配件，额外物品，多余装饰'),
 'character': ('【角色一致·最高优先】必须是参考图中的同一个人/角色：脸型、五官、发型、年龄、身材比例、画风与身上服饰与参考图一致，不得换成另一个人；'
               '姿态与表情严格执行本张文字要求，不要照搬参考图的转头角度、手臂动作、构图或背景。',
               '换成另一个人，改变脸型，改变发型，改变服饰，照搬参考图的姿势，照搬参考图的表情，多个角色'),
}
def _lock(kind):
    import overrides
    return overrides.lock(kind) or LOCKS.get(kind, LOCKS['item'])
def constrain(description, has_refs, custom=False, kind='item'):
    description = description.strip()
    lock = _lock(kind)[0]
    if has_refs and not custom and lock not in description:
        description = description + '\n' + lock
    return description
MAX_IMAGE = 25 * 1024 * 1024
MATERIAL_ALGORITHM = 'qwen-material-prepare-v1'
CACHE_ID = re.compile(r'[0-9a-f]{64}\Z')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode('utf-8')).hexdigest()


def material_model_signature(root=HERE.parent):
    receipt = json.loads((root/'model-verification.json').read_text())
    if receipt.get('verified') is not True: raise ValueError('模型校验尚未完成')
    files = [{name: entry[name] for name in ('path', 'size', 'sha256')}
             for entry in receipt['files']]
    return digest({'algorithm': MATERIAL_ALGORITHM,
                   'files': sorted(files, key=lambda entry: entry['path'])})


def material_digest(description, size, references, signature):
    return digest({'description': description.strip(), 'size': size,
                   'references': references, 'model_signature': signature})


def material_shape_matches(width, height, shape):
    """Check the native Qwen area resize, with at most half a 32px rounding step.

    The first reference supplies aspect ratio; unreferenced outputs are square.
    This rejects wrong-resolution receipts without guessing reference geometry.
    """
    if (not isinstance(shape, dict) or type(shape.get('resolution')) is not int
            or any(type(dim) is not int or not 32 <= dim <= 16384 or dim % 32
                   for dim in (width, height))):
        return False
    resolution = shape['resolution']
    if shape.get('square'):
        return width == height == resolution
    # Values below one grid cell are clamped to 32 by the encoder.
    lower = (width - 16 if width > 32 else 0) * (height - 16 if height > 32 else 0)
    upper = (width + 16) * (height + 16)
    return lower <= resolution * resolution <= upper


class MaterialUpgradeRequired(ValueError):
    def __init__(self):
        super().__init__('整包准备组件已更新，当前引擎尚未加载；请本次任务暂停后重新打开工作台')

class BackendRejected(ValueError):
    """An explicit non-acceptance response, not a lost or malformed reply."""

def download_message(root=HERE.parent):
    manifest = json.loads((root/'model-manifest.json').read_text())
    total = sum(item['size'] for item in manifest['files'])
    downloaded = 0
    logs = ('download-main-status.log', 'download-encoder-status.log', None)
    for item, log in zip(manifest['files'], logs):
        path = Path(item['path'])
        if path.is_file() and path.stat().st_size == item['size'] and not path.with_name(path.name+'.aria2').exists():
            downloaded += item['size']
        elif log and (root/log).exists():
            with (root/log).open('rb') as stream:
                stream.seek(max(0, (root/log).stat().st_size-4096))
                percentages = re.findall(r'\((\d+)%\)', stream.read().decode(errors='replace'))
            if percentages: downloaded += item['size'] * int(percentages[-1]) / 100
    return f'模型文件已下载约 {downloaded/1e9:.1f} / {total/1e9:.1f} GB（{downloaded/total:.0%}），等待下载和校验完成。'

def require_models(root=HERE.parent):
    """Trust the installer's hash receipt; cheaply recheck identity and size."""
    try:
        manifest = json.loads((root/'model-manifest.json').read_text())
        receipt = json.loads((root/'model-verification.json').read_text())
        if receipt.get('verified') is not True or receipt.get('revision') != manifest.get('revision'):
            raise ValueError('模型校验尚未完成')
        files = manifest['files']; verified = {v['path']:v for v in receipt['files']}
        if len(files) != 3 or len(verified) != 3: raise ValueError('模型文件不完整')
        for item in files:
            path = Path(item['path']); proof = verified.get(str(path),{})
            models_root = (root/'models').resolve()
            # absolute() does not follow the final symlink; resolve() may leave models/
            under_models = path.absolute().is_relative_to(models_root) or path.resolve().is_relative_to(models_root)
            if (not under_models
                or not path.is_file() or path.stat().st_size != item['size']
                or path.with_name(path.name+'.aria2').exists()
                or proof.get('size') != item['size'] or proof.get('sha256') != item['sha256']):
                raise ValueError('模型文件仍在下载或需要重新校验')
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError('模型正在下载，等待完整文件校验。') from error

def safe_name(value, *, folder=False):
    value = str(value)
    if not value and folder: return ''
    p = PurePosixPath(value)
    if not value or '\\' in value or '\x00' in value or p.is_absolute() or '..' in p.parts:
        raise ValueError('文件位置不合法')
    if not folder and len(p.parts) != 1: raise ValueError('文件名不合法')
    return value


CHAR_LORA_FILE = "ohwxchar.safetensors"
CHAR_LORA_TRIGGER = "ohwxchar"
CHAR_LORA_STRENGTH = 0.85
POSE_ONLY_PROMPT = (
    "Same-image pose edit: keep the EXACT same person, face, hair, skin, and the EXACT same clothes "
    "(color, fabric, neckline, sleeves, logos, accessories). Do NOT change or invent a new outfit. "
    "Image 1 is a face crop for identity only. Image 2 shows the same outfit/hair/background with arms removed — copy the clothes, ignore its missing arms. "
    "Change ONLY the body pose to match the pose-control skeleton (arms, elbows, wrists, hands, stance). "
    "Hands: open palms with clearly separated fingers when the skeleton shows open hands; no clenched fists, no missing fingers. "
    "Exactly one head, one torso, two arms, two legs. Keep lighting and background. Pose can be approximate."
)

def char_lora_ready():
    return (Path("/root/autodl-tmp/ComfyUI/models/loras") / CHAR_LORA_FILE).is_file()

def ensure_trigger(text, trigger=CHAR_LORA_TRIGGER):
    t = (text or "").strip()
    if not t:
        return t
    low = t.lower()
    if low.startswith(trigger.lower() + ",") or low.startswith(trigger.lower() + " ") or low.split(",", 1)[0].strip() == trigger.lower():
        return t
    return f"{trigger}, {t}"

def apply_char_lora(graph, strength=None):
    """UNET(1) -> LoraLoaderModelOnly(20) -> KSampler/ControlNet."""
    if not char_lora_ready():
        raise ValueError("角色 LoRA 文件不存在，请先完成训练或重新挂载")
    if "20" in graph and graph["20"].get("class_type") == "LoraLoaderModelOnly":
        return graph
    st = CHAR_LORA_STRENGTH if strength is None else float(strength)
    if not (0.0 <= st <= 2.0):
        raise ValueError("LoRA 强度需在 0–2 之间")
    ks = graph.get("5")
    if not ks or ks.get("class_type") != "KSampler":
        raise ValueError("当前任务图不支持角色 LoRA")
    consumers = []
    if ks["inputs"].get("model") == ["1", 0]:
        consumers.append(("5", "model"))
    for nid, node in list(graph.items()):
        if node.get("class_type") == "ZImageFunControlnet" and node.get("inputs", {}).get("model") == ["1", 0]:
            consumers.append((nid, "model"))
    graph["20"] = {"class_type": "LoraLoaderModelOnly",
                   "inputs": {"model": ["1", 0], "lora_name": CHAR_LORA_FILE, "strength_model": st}}
    if not consumers:
        ks["inputs"]["model"] = ["20", 0]
    else:
        for nid, key in consumers:
            graph[nid]["inputs"][key] = ["20", 0]
    return graph

class GraphBuilder:
    """Only a reviewed local graph is runnable. Missing bindings fail closed."""
    def __init__(self, path=TEMPLATE): self.path = Path(path)
    def read(self):
        if not self.path.is_file(): raise ValueError('模型任务图尚未接通，请等待安装完成。')
        result = json.loads(self.path.read_text())
        if not isinstance(result.get('workflows'), dict): raise ValueError('任务图配置不完整')
        return result
    def build(self, description, size, references, custom=False, kind='item', use_lora=False, lora_strength=None, pose_only=False):
        if pose_only:
            description = POSE_ONLY_PROMPT
        elif not isinstance(description, str) or not description.strip() or len(description) > 12000:
            raise ValueError('请填写描述，长度不超过 12000 字。')
        if type(size) is not int or not 512 <= size <= 2048 or size % 32:
            raise ValueError('画质尺寸需为 512–2048 之间的整数，按 32 像素递增。')
        if len(references) > 10: raise ValueError('一次最多使用 10 张参考照片')
        spec = self.read()['workflows']['reference_edit' if references else 't2i']
        graph = copy.deepcopy(spec['prompt']); bindings = spec['bindings']
        def bind(name, value):
            node, key = bindings[name]
            graph[str(node)]['inputs'][key] = value
        if pose_only:
            # 同图重绘：不要追加「勿照搬构图/背景」的角色锁，否则会鼓励重画场景
            bind('text', description)
            if references and 'negative_text' in bindings:
                node, key = bindings['negative_text']
                graph[str(node)]['inputs'][key] = (
                    graph[str(node)]['inputs'].get(key, '') + '，' + _lock('character')[1]
                    + '，改变背景，重绘场景，换成另一套衣服'
                )
        else:
            bind('text', constrain(description, bool(references), custom, kind))
            if references and not custom and 'negative_text' in bindings:
                node, key = bindings['negative_text']
                graph[str(node)]['inputs'][key] = graph[str(node)]['inputs'].get(key, '') + '，' + _lock(kind)[1]
        bind('seed', secrets.randbits(32))
        # Preserve reviewed presets. Custom values are native pixel-area sizes
        # only for this explicitly reviewed Qwen 2.1 encoder binding; never infer
        # the meaning of a resolution input on another graph or model.
        choices = bindings.get('resolution_values', {})
        if size in (1024, 1536, 2048):
            if str(size) not in choices: raise ValueError('这个画质尚未配置完成，请选择已就绪的画质。')
            resolution = choices[str(size)]
        else:
            if (bindings.get('resolution') != ['4', 'resolution']
                    or graph.get('4', {}).get('class_type') != 'TextEncodeQwenImage21'
                    or 'resolution' not in graph['4'].get('inputs', {})):
                raise ValueError('当前任务图尚未支持自定义画质，请使用已审核的 Qwen 2.1 配置。')
            resolution = size
        bind('resolution', resolution)
        if references:
            text_node = str(bindings['text'][0])
            for index, reference in enumerate(references):
                node = str(8 + index)
                graph[node] = {'class_type':'LoadImage', 'inputs':{'image':reference}}
                graph[text_node]['inputs'][f'images.image_{index+1}'] = [node, 0]
        if use_lora:
            text_node = str(bindings['text'][0])
            graph[text_node]['inputs']['prompt'] = ensure_trigger(graph[text_node]['inputs'].get('prompt', ''))
            apply_char_lora(graph, lora_strength)
        return graph

    def prepare(self, items, request_id, model_signature):
        if not isinstance(items, list) or not 1 <= len(items) <= 100:
            raise ValueError('整包准备一次需要 1 至 100 张图片')
        result, image_nodes, plan_items, keys = {}, {}, [], set()
        for item in items:
            key = item.get('key')
            if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', key) or key in keys:
                raise ValueError('素材编号无效或重复')
            keys.add(key)
            graph = self.build(item.get('description'), item.get('size'), item.get('references', []))
            if not result:
                if graph['2']['class_type'] != 'CLIPLoader' or graph['3']['class_type'] != 'VAELoader':
                    raise ValueError('整包准备所需的模型任务图尚未接通')
                result = {'2': graph['2'], '3': graph['3']}
            image_keys = []
            for reference in item.get('references', []):
                if reference not in image_nodes:
                    if len(image_nodes) >= 16: raise ValueError('整包最多使用 16 张不同的参考照片')
                    image_nodes[reference] = f'image_{len(image_nodes)+1}'
                image_keys.append(image_nodes[reference])
            plan_items.append({'key': key, 'prompt': graph['4']['inputs']['prompt'],
                'resolution': graph['4']['inputs']['resolution'], 'image_keys': image_keys})
        plan = {'version': 1, 'model_signature': model_signature, 'request_tag': request_id, 'items': plan_items}
        inputs = {'clip': ['2', 0], 'vae': ['3', 0],
                  'plan_json': json.dumps(plan, ensure_ascii=False, separators=(',', ':'))}
        for index, (filename, key) in enumerate(image_nodes.items(), 8):
            result[str(index)] = {'class_type': 'LoadImage', 'inputs': {'image': filename}}
            inputs[f'images.{key}'] = [str(index), 0]
        result['4'] = {'class_type': 'QwenMaterialPreparePack', 'inputs': inputs}
        return result

    def prepared(self, description, size, references, cache_id, model_signature):
        graph = self.build(description, size, references)
        sampler = graph['5']
        expected_inputs = {'model', 'seed', 'steps', 'cfg', 'sampler_name', 'scheduler',
                           'positive', 'negative', 'latent_image', 'denoise'}
        if (sampler['class_type'] != 'KSampler' or set(sampler['inputs']) != expected_inputs
                or type(sampler['inputs']['cfg']) not in (int, float) or sampler['inputs']['cfg'] != 1
                or sampler['inputs']['model'] != ['1', 0]
                or graph['1']['class_type'] != 'UNETLoader'
                or any(node['class_type'] not in {'UNETLoader', 'CLIPLoader', 'VAELoader',
                    'TextEncodeQwenImage21', 'KSampler', 'VAEDecode', 'SaveImage', 'LoadImage',
                    'LoraLoaderModelOnly', 'LoraLoader'}
                    for node in graph.values())):
            raise ValueError('当前任务图不支持已准备内容，请使用已审核的原始采样配置')
        graph.pop('2', None)
        graph = {key: value for key, value in graph.items() if value['class_type'] != 'LoadImage'}
        graph['4'] = {'class_type': 'QwenMaterialLoadPrepared',
                      'inputs': {'cache_id': cache_id, 'model_signature': model_signature},
                      '_meta': {'qwen_material_submission_hash': hashlib.sha256(description.strip().encode('utf-8')).hexdigest()}}
        return graph

async def backend(app, method, path, **kwargs):
    async with app['http'].request(method, BACKEND + path, **kwargs) as response:
        data = await response.json(content_type=None) if response.content_type == 'application/json' else await response.text()
        if response.status >= 500:
            raise ClientError('本地模型服务暂时没有正常响应')
        if response.status >= 400:
            raise BackendRejected('本地模型返回错误：' + str(data)[:1600])
        return data


@web.middleware
async def gzip_middleware(request, handler):
    response = await handler(request)
    if response.status != 200 or 'Content-Encoding' in response.headers: return response
    if not isinstance(response, web.Response): return response  # skip FileResponse/StreamResponse
    ctype = (response.content_type or '').split(';')[0].strip()
    if ctype not in ('text/html', 'text/css', 'text/plain', 'application/json', 'application/javascript'): return response
    body = response.body
    if not isinstance(body, (bytes, bytearray)) or len(body) < 500: return response
    if 'gzip' not in request.headers.get('Accept-Encoding', ''): return response
    import gzip
    gz = gzip.compress(bytes(body), compresslevel=5)
    if len(gz) >= len(body): return response
    headers = {k: v for k, v in response.headers.items() if k.lower() not in ('content-length', 'content-encoding')}
    headers['Content-Encoding'] = 'gzip'
    headers['Vary'] = 'Accept-Encoding'
    return web.Response(body=gz, status=200, headers=headers)

@web.middleware
async def guard(request, handler):
    # login endpoint is public
    if request.method == 'POST' and request.path in ('/api/login', '/login'):
        return await handler(request)
    given = None if NO_TOKEN else request.query.get('token')
    if given is not None and secrets.compare_digest(given, ACCESS_TOKEN) and request.method == 'GET' and request.path == '/':
        response = web.HTTPFound('/')
        set_auth_cookie(response, request)
        raise response
    if not NO_TOKEN and not secrets.compare_digest(request.cookies.get('studio_auth_v2', ''), ACCESS_TOKEN):
        # browser navigations get a login page; APIs get JSON
        accept = request.headers.get('Accept', '')
        if request.method in ('GET', 'HEAD') and request.path == '/':
            return web.Response(text=LOGIN_PAGE, content_type='text/html',
                                headers={'Cache-Control': 'no-store', 'X-Frame-Options': 'DENY'})
        return web.json_response({'error':'需要访问密码'}, status=403)
    origin = request.headers.get('Origin')
    if origin and origin.split('://', 1)[-1] != request.host: return web.json_response({'error':'来源不允许'}, status=403)
    if request.path.startswith('/api/'):
        owner = request.cookies.get('studio_session')
        hdr = request.headers.get('X-Studio-Token')
        # 服务重启后 owners 会空：cookie/内嵌 token 重新认回，避免手机报本地服务没有正常响应
        if owner:
            request.app['owners'].add(owner)
        if hdr:
            request.app['owners'].add(hdr)
        if not owner:
            owner = hdr if hdr else secrets.token_urlsafe(32)
            request.app['owners'].add(owner)
            request['_set_session'] = owner
        if request.method != 'GET' and hdr and owner and hdr != owner:
            owner = hdr
            request.app['owners'].add(owner)
        request['owner'] = owner or hdr
    try:
        resp = await handler(request)
        if request.get('_set_session') and request.path != '/api/logout':
            resp.set_cookie('studio_session', request['_set_session'], httponly=True, samesite='Lax')
        return resp
    except MaterialUpgradeRequired as error:
        return web.json_response({'error': str(error), 'code': 'prepare_upgrade_required'}, status=400)
    except (ValueError, KeyError, TypeError) as error: return web.json_response({'error':str(error)}, status=400)
    except (OSError, ClientError, asyncio.TimeoutError):
        return web.json_response({'error':'本地模型服务尚未连接，稍后重试。'}, status=503)

async def login_form(request):
    if NO_TOKEN:
        raise web.HTTPFound('/')
    data = await request.post()
    pw = data.get('password', '')
    if not isinstance(pw, str) or not secrets.compare_digest(pw, ACCESS_TOKEN):
        bad = LOGIN_PAGE.replace('id="err"></div>', 'id="err">密码错误</div>', 1)
        return web.Response(text=bad, content_type='text/html', status=403,
                            headers={'Cache-Control': 'no-store'})
    resp = web.HTTPFound('/')
    set_auth_cookie(resp, request)
    raise resp

async def login(request):
    if NO_TOKEN:
        return web.json_response({'ok': True, 'auth': False})
    try:
        data = await request.json()
    except Exception:
        data = {}
    pw = data.get('password') if isinstance(data, dict) else None
    if not isinstance(pw, str) or not secrets.compare_digest(pw, ACCESS_TOKEN):
        return web.json_response({'error': '密码错误'}, status=403)
    resp = web.json_response({'ok': True})
    set_auth_cookie(resp, request)
    return resp


async def video_library_delete(request):
    import video
    data = await request.json()
    names = data.get('names')
    if not isinstance(names, list) or not names or len(names)>1000:
        raise ValueError('请选择要删除的视频')
    paths=[]
    for name in set(names):
        name=video._vname(name)
        path=video.VIDEOS/name
        if path.is_symlink(): raise ValueError('不能删除链接文件')
        for key in ('extract_state', 'train_state'):
            st=request.app.get(key) or {}
            if st.get('state')=='running' and st.get('video')==name:
                raise ValueError('视频正在使用，请先停止任务')
        paths.append(path)
    for path in paths:
        path.unlink()
        video.delete_cover(path.name)
    return web.json_response({'ok':True, 'deleted':len(paths)})

async def studio_logout(request):
    owner=request.cookies.get('studio_session')
    request.app['owners'].discard(owner)
    resp=web.json_response({'ok':True})
    resp.del_cookie('studio_auth_v2',path='/'); resp.del_cookie('studio_auth',path='/'); resp.del_cookie('studio_session',path='/')
    return resp

async def index(request):
    owner = request.cookies.get('studio_session')
    if owner not in request.app['owners']:
        owner = secrets.token_urlsafe(32); request.app['owners'].add(owner)
    response = web.Response(text=(HERE/'index.html').read_text().replace('__STUDIO_TOKEN__', owner), content_type='text/html')
    response.set_cookie('studio_session', owner, httponly=True, samesite='Lax')
    response.headers.update({'X-Qwen-Material-Prepare':'4', 'Cache-Control':'no-store', 'X-Frame-Options':'DENY', 'Referrer-Policy':'no-referrer'})
    response.headers['Content-Security-Policy'] = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self' blob:; connect-src 'self'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'"
    return response

async def lora_status(request):
    ready = char_lora_ready()
    return web.json_response({'ready': ready, 'file': CHAR_LORA_FILE if ready else None,
                              'trigger': CHAR_LORA_TRIGGER, 'strength_default': CHAR_LORA_STRENGTH})

async def health(request):
    try:
        require_models()
        spec = request.app['builder'].read()
        sizes = spec['workflows']['t2i']['bindings'].get('resolution_values', {})
        await backend(request.app, 'GET', '/system_stats')
        return web.json_response({'ready':bool(sizes), 'message':'本地模型服务已连接', 'sizes':list(sizes)})
    except Exception as error:
        message = str(error) if isinstance(error, ValueError) else '正在等待本地模型服务启动。'
        if not (HERE.parent/'model-verification.json').exists():
            try: message = download_message()
            except (OSError, KeyError, ValueError): pass
        return web.json_response({'ready':False, 'message':message, 'sizes':[]})

async def upload(request):
    reader = await request.multipart(); part = await reader.next()
    if not part or part.name != 'image': raise ValueError('请选择参考照片')
    data = bytearray()
    while chunk := await part.read_chunk():
        data.extend(chunk)
        if len(data) > MAX_IMAGE: raise ValueError('单张参考照片不得超过 25 MB')
    ext = 'png' if data[:8] == b'\x89PNG\r\n\x1a\n' else 'jpg' if data[:3] == b'\xff\xd8\xff' else 'webp' if data[:4] == b'RIFF' and data[8:12] == b'WEBP' else None
    if not ext: raise ValueError('请使用 PNG、JPG 或 WebP 照片')
    from aiohttp import FormData
    name = 'studio-' + secrets.token_hex(16) + '.' + ext
    form = FormData(); form.add_field('image', bytes(data), filename=name, content_type='image/'+ext)
    result = await backend(request.app, 'POST', '/upload/image', data=form)
    filename = safe_name(result['name']); folder = safe_name(result.get('subfolder',''), folder=True)
    if result.get('type','input') != 'input': raise ValueError('参考照片没有保存到输入目录')
    key = secrets.token_urlsafe(18)
    request.app['references'][key] = (request['owner'], '/'.join(v for v in (folder,filename) if v))
    return web.json_response({'id':key})

async def upload_existing(request):
    # 素材库里已有的照片直接引用，不再重新上传出一份 studio-xxx 副本
    import pack
    data = await request.json()
    hist = data.get('history') if isinstance(data, dict) else None
    if hist is not None:
        # 单张生成历史（outputs/Qwen21）里的原图，ComfyUI LoadImage 用 [output] 标注读取
        import library
        try: hist = library._hist_name(hist)
        except web.HTTPNotFound: raise ValueError('找不到历史图片') from None
        name = 'Qwen21/' + hist + ' [output]'
    else:
        name = pack._input(data.get('name') if isinstance(data, dict) else None)
    key = secrets.token_urlsafe(18)
    request.app['references'][key] = (request['owner'], name)
    return web.json_response({'id':key})

_UNCHANGED = object()
_NODE_STAGES = {
    'UNETLoader': ('load_model', '载入图像模型'),
    'LoraLoaderModelOnly': ('load_model', '载入角色 LoRA'),
    'LoraLoader': ('load_model', '载入角色 LoRA'),
    'UnetLoaderGGUF': ('load_model', '载入图像模型'),
    'UNETLoader': ('load_model', '载入图像模型'),
    'CLIPLoader': ('load_model', '载入理解模型'),
    'VAELoader': ('load_model', '载入图片处理模型'),
    'LoadImage': ('encode', '读取参考照片'),
    'ModelPatchLoader': ('load_model', '载入姿势控制模型'),
    'ZImageFunControlnet': ('encode', '接入动作骨架'),
    'EmptyLatentImage': ('encode', '按动作图定画面比例'),
    'TextEncodeQwenImage21': ('encode', '理解描述与参考照片'),
    'QwenMaterialPreparePack': ('prepare', '准备整包素材'),
    'QwenMaterialLoadPrepared': ('encode', '读取已准备内容'),
    'KSampler': ('sampling', '图像采样'),
    'KSamplerAdvanced': ('sampling', '图像采样'),
    'VAEDecode': ('decode', '还原高清图片'),
    'VAEDecodeTiled': ('decode', '还原高清图片'),
    'SaveImage': ('save', '保存图片'),
}


def telemetry(job, *, stage=None, label=None, current=_UNCHANGED, total=_UNCHANGED,
              unit=None, connected=None, queue_ahead=_UNCHANGED, new_node=False):
    """Publish real changes; a poll or a connection change is not work progress."""
    previous = job.get('telemetry') or {}
    stage = stage or previous.get('stage', 'queued')
    label = label or previous.get('label', '等待本地任务')
    changed_stage = (not previous or previous.get('stage') != stage
                     or previous.get('label') != label or new_node)
    now = time.time()
    if changed_stage:
        result = {'stage': stage, 'label': label, 'stage_started_at': now,
                  'updated_at': now, 'connected': previous.get('connected', False)}
        job.update(current=None, total=None)
    else:
        result = dict(previous)
    if connected is not None:
        result['connected'] = bool(connected)
    if current is not _UNCHANGED and total is not _UNCHANGED:
        # Negative, fractional, decreasing and contradictory counters are not
        # progress. A new node/phase starts a fresh counter above.
        if (type(current) is int and type(total) is int and 0 <= current <= total <= 1000000
                and total > 0 and unit in ('步', '项')
                and ('total' not in result or result['total'] == total)
                and current >= result.get('current', 0)):
            if result.get('current') != current or result.get('total') != total:
                result['updated_at'] = now
            result.update(current=current, total=total, unit=unit)
            job.update(current=current, total=total)
    if stage != 'queued':
        result.pop('queue_ahead', None)
    elif queue_ahead is not _UNCHANGED and type(queue_ahead) is int and queue_ahead >= 0:
        old = result.get('queue_ahead')
        if old is None or queue_ahead < old:
            result['updated_at'] = now
        result['queue_ahead'] = queue_ahead
    job['telemetry'] = result
    return result


def execution_node(job, node):
    if not isinstance(node, (str, int)) or isinstance(node, bool):
        return None
    node = str(node)
    node_type = job.get('_node_types', {}).get(node)
    if not isinstance(node_type, str):
        stage, label = ('processing', '正在处理')
    else:
        stage, label = _NODE_STAGES.get(node_type, ('processing', '处理本地任务'))
    changed = job.get('_active_node') != node
    job['_active_node'] = node
    job.update(state='running', message=label + '…')
    telemetry(job, stage=stage, label=label, new_node=changed)
    return node_type


def _step_pair(value, maximum):
    if isinstance(value, bool) or isinstance(maximum, bool):
        return None
    if isinstance(value, float):
        value = int(value)
    if isinstance(maximum, float):
        maximum = int(round(maximum))
    if type(value) is int and type(maximum) is int and 0 <= value <= maximum <= 1000000 and maximum > 0:
        return value, maximum
    return None


def _publish_steps(job, value, maximum, label='图像采样'):
    pair = _step_pair(value, maximum)
    if not pair:
        return False
    value, maximum = pair
    progress = telemetry(job, stage='sampling', label=label, current=value, total=maximum, unit='步')
    if 'current' in progress:
        job['state'] = 'running'
        job['message'] = f"图像采样：第 {progress['current']} / {progress['total']} 步"
        return True
    return False


def _clarify_job_message(job):
    msg = str(job.get('message') or '')
    tel = job.get('telemetry') or {}
    generic = ('本地任务已开始' in msg) or ('等待模型处理' in msg)
    cur, tot = tel.get('current'), tel.get('total')
    if generic and _step_pair(cur, tot):
        job['message'] = f"{tel.get('label') or '图像采样'}：第 {int(cur)} / {int(tot)} 步"
    elif generic and job.get('state') == 'running':
        lab = tel.get('label')
        if lab and tel.get('stage') not in (None, 'starting', 'queued'):
            job['message'] = str(lab) + '…'
        else:
            job['message'] = '已接到任务，正在载入模型，采样步数马上显示…'


def watch_progress(job, kind, data):
    if kind == 'progress_state' and isinstance(data, dict):
        nodes = data.get('nodes')
        best = None
        if isinstance(nodes, dict):
            for nid, info in nodes.items():
                if not isinstance(info, dict):
                    continue
                pair = _step_pair(info.get('value', info.get('current')), info.get('max', info.get('total')))
                if not pair:
                    continue
                ntype = job.get('_node_types', {}).get(str(nid))
                if ntype in ('KSampler', 'KSamplerAdvanced') or best is None:
                    best = pair
                    if ntype in ('KSampler', 'KSamplerAdvanced'):
                        break
        if best:
            _publish_steps(job, best[0], best[1])
        return
    node = data.get('node')
    node_s = str(node) if isinstance(node, (str, int)) and not isinstance(node, bool) else None
    active = job.get('_active_node')
    node_type = job.get('_node_types', {}).get(node_s) if node_s else None
    if node_s is not None and active and node_s != active:
        if node_type not in ('KSampler', 'KSamplerAdvanced', 'QwenMaterialPreparePack'):
            return
    if node_type is None and active:
        node_type = job.get('_node_types', {}).get(active)
    pair = _step_pair(data.get('value'), data.get('max'))
    if not pair:
        return
    value, maximum = pair
    if kind == 'qwen_material_progress' and node_type == 'QwenMaterialPreparePack':
        if maximum != len(job.get('prepare_proofs', {})): return
        if value < job.get('_prepare_completed', 0) or not isinstance(data.get('detail'), str): return
        label = {'references': '准备参考照片', 'description': '理解素材描述',
                 'cache': '保存准备内容', 'saved': '已准备素材'}.get(data.get('detail'))
        if label is None: return
        job['_prepare_completed'] = value
        telemetry(job, stage='prepare', label=label)
        unit = '项'
    elif kind == 'progress' and (node_type in ('KSampler', 'KSamplerAdvanced') or node_type is None):
        _publish_steps(job, value, maximum, '图像采样')
        return
    else:
        # The prepare node can emit nested VAE tile/encoder counters. Only its
        # explicit item-receipt event proves how many materials are prepared.
        return
    progress = telemetry(job, current=value, total=maximum, unit=unit)
    if 'current' in progress:
        job['message'] = f"{progress['label']}：{progress['current']} / {progress['total']} {unit}…"


async def watch(app, job, ws):
    telemetry(job, connected=True)
    job.pop('connection_note', None)
    try:
        async for message in ws:
            if message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR): break
            if message.type != WSMsgType.TEXT: continue
            try: event = json.loads(message.data)
            except (TypeError, ValueError): continue
            if not isinstance(event, dict): continue
            kind, data = event.get('type'), event.get('data')
            if not isinstance(data, dict): continue
            _pid = data.get('prompt_id')
            if _pid is not None and _pid != (job.get('prompt_id') or job['id']): continue
            if job.get('state') in ('done', 'error', 'cancelled'): break
            if kind == 'execution_start':
                job.update(state='running', message='已接到任务，正在载入模型…')
                telemetry(job, stage='starting', label='已接到任务')
            elif kind == 'executing':
                execution_node(job, data.get('node'))
            elif kind in ('progress', 'qwen_material_progress', 'progress_state'):
                watch_progress(job, kind, data)
            elif kind == 'execution_error':
                job.update(state='error', message=str(data.get('exception_message', '生成失败'))[:1600])
                telemetry(job, stage='error', label='本地任务失败')
                break
            elif kind == 'execution_interrupted':
                job.update(state='cancelled', message='已取消本次生成')
                telemetry(job, stage='cancelled', label='任务已取消')
                break
            elif kind == 'execution_success':
                # The sampler and graph finishing are not a verified image or
                # a complete prepared receipt. /status checks actual history.
                stage = 'prepare' if job.get('kind') == 'material_prepare' else 'save'
                label = '核验整包准备结果' if stage == 'prepare' else '核验已保存结果'
                job.update(state='running', message=label + '…')
                telemetry(job, stage=stage, label=label)
                break
    except (Exception, asyncio.CancelledError):
        pass
    finally:
        telemetry(job, connected=False)
        if job.get('state') not in ('done', 'error', 'cancelled'):
            job['connection_note'] = '实时进度连接已断开，正在查询实际结果。'
        try: await ws.close()
        except Exception: pass

def submission_start(app, owner, request_id, *, fingerprint=None, allow_queue=False):
    """Idempotent start. allow_queue=True lets another generate join the Comfy queue."""
    if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', request_id):
        raise ValueError('任务标识无效')
    previous = app['submissions'].get((owner, request_id))
    if previous and fingerprint is not None and previous.get('_fingerprint') != fingerprint:
        raise ValueError('任务标识已用于其他内容，请为新任务使用新的标识')
    if previous is None and fingerprint is not None:
        # Only coalesce identical preparation that is still active, and only
        # within this owner. A later explicit run may need to rebuild its cache.
        for (saved_owner, _), candidate in list(app['submissions'].items()):
            if saved_owner != owner or candidate.get('_fingerprint') != fingerprint:
                continue
            state = candidate.get('state')
            active = state in ('submitting', 'unknown') or (state == 'submitted'
                and app['jobs'].get(candidate.get('id'), {}).get('state') in ('queued', 'running'))
            if active:
                previous = candidate
                app['submissions'][(owner, request_id)] = candidate
                break
    if previous:
        if previous.get('id'): return previous['id']
        if fingerprint is not None and previous.get('state') == 'submitting':
            return previous
        # Same request_id retried — idempotent, not a new queue slot
        raise ValueError(previous.get('error') or '本次任务已经提交，请查询进度，不要重复生成。')
    if any(key[0] == owner and value['state'] in ('submitting','unknown')
           for key, value in app['submissions'].items()):
        raise ValueError('上一任务的提交状态尚未确认，请先恢复该任务。')
    if not allow_queue:
        if any(j['owner']==owner and j['state'] in ('queued','running','cancelling') for j in app['jobs'].values()):
            raise ValueError('请等待或取消当前生成，再开始下一张。')
    return None


def owned_references(app, owner, values):
    if not isinstance(values, list) or len(values) > 10:
        raise ValueError('一次最多使用 10 张已上传的参考照片')
    refs = []
    for key in values:
        if not isinstance(key, str): raise ValueError('参考照片编号无效')
        saved = app['references'].get(key)
        if not saved or saved[0] != owner: raise ValueError('参考照片已失效，请重新上传')
        refs.append(saved[1])
    return refs


async def material_nodes_ready(app):
    for name in ('QwenMaterialPreparePack', 'QwenMaterialLoadPrepared'):
        try: info = await backend(app, 'GET', '/object_info/' + name)
        except BackendRejected: raise MaterialUpgradeRequired() from None
        if not isinstance(info, dict) or name not in info: raise MaterialUpgradeRequired()
        if name == 'QwenMaterialPreparePack':
            inputs = info[name].get('input', {}) if isinstance(info[name], dict) else {}
            control = next((inputs.get(group, {}).get('emit_progress')
                            for group in ('required', 'optional') if isinstance(inputs.get(group), dict)
                            and 'emit_progress' in inputs[group]), None)
            if not isinstance(control, (list, tuple)) or not control or control[0] != 'BOOLEAN':
                raise MaterialUpgradeRequired()


async def submit_graph(app, owner, request_id, graph, *, metadata=None, fingerprint=None):
    # No await between duplicate guard and reservation. Concurrent identical
    # preparations share one acknowledgement and never enqueue a second graph.
    previous = submission_start(app, owner, request_id, fingerprint=fingerprint)
    if isinstance(previous, str): return web.json_response({'id': previous})
    if isinstance(previous, dict):
        await previous['_settled'].wait()
        if previous.get('id'): return web.json_response({'id': previous['id']})
        if previous.get('state') == 'unknown':
            raise ClientError('提交结果尚未确认，请恢复原任务。')
        raise ValueError(previous.get('error') or '提交失败，请查询原任务状态')
    client = secrets.token_hex(16)
    metadata = dict(metadata or {})
    # Retain only node types for telemetry, never the submitted prompt or graph.
    metadata['_node_types'] = {str(node_id): node['class_type'] for node_id, node in graph.items()
                               if isinstance(node, dict) and isinstance(node.get('class_type'), str)}
    submission = {'state':'submitting', 'client':client, 'metadata': metadata,
                  '_fingerprint': fingerprint, '_settled': asyncio.Event()}
    app['submissions'][(owner, request_id)] = submission
    ws, sent = None, False
    try:
        ws = await app['http'].ws_connect(BACKEND + '/ws?clientId=' + client, heartbeat=30)
        sent = True
        result = await backend(app, 'POST', '/prompt', json={'prompt':graph, 'client_id':client})
        if not result.get('prompt_id'): raise BackendRejected('模型未接受任务：'+str(result)[:1600])
        job = {'id':result['prompt_id'], 'owner':owner, 'state':'queued', 'message':'已提交，正在排队', 'images':[], **metadata}
        telemetry(job, stage='queued', label='等待本地任务', connected=True)
        app['jobs'][job['id']] = job
        submission.update(state='submitted', id=job['id'])
        task = asyncio.create_task(watch(app, job, ws)); app['tasks'].add(task); task.add_done_callback(app['tasks'].discard)
        return web.json_response({'id':job['id']})
    except (Exception, asyncio.CancelledError) as error:
        rejected = isinstance(error, BackendRejected) or not sent
        submission.update(state='failed' if rejected else 'unknown',
                          error='提交失败' if rejected else '提交结果尚未确认，正在恢复原任务。')
        if ws is not None: await ws.close()
        if isinstance(error, asyncio.CancelledError): raise
        if not rejected: raise ClientError('提交结果尚未确认，请恢复原任务。') from error
        raise
    finally:
        submission['_settled'].set()



async def _generate_worker(app, owner, request_id, data, refs):
    """Heavy path: identity crops + Comfy submit. Updates placeholder job in place."""
    job = app['jobs'].get(request_id)
    try:
        prepared_id = data.get('prepared_id')
        if prepared_id is not None:
            if not isinstance(prepared_id, str) or not CACHE_ID.fullmatch(prepared_id):
                raise ValueError('已准备内容编号无效')
            signature = material_model_signature()
            proof = app.get('prepared_permissions', {}).get((owner, prepared_id))
            checked = app['builder'].build(data.get('description'), data.get('size'), refs)
            expected = material_digest(data['description'], data['size'], refs, signature)
            if (not proof or expected not in proof['digests'] or proof['model_signature'] != signature
                    or proof.get('resolution') != checked['4']['inputs']['resolution']):
                raise ValueError('已准备内容不属于当前素材或会话，请重新准备整包')
            graph = app['builder'].prepared(data['description'], data['size'], refs, prepared_id, signature)
            graph['4']['_meta']['qwen_material_request_id'] = request_id
            if data.get('use_lora', False) is True and char_lora_ready():
                apply_char_lora(graph, data.get('lora_strength'))
        else:
            pose_only = data.get('pose_only') is True
            if pose_only:
                data = dict(data)
                # Honor UI strength (default 0.7). Old code forced >=1.25 and washed out identity.
                raw = data.get('pose_strength')
                try:
                    s = 0.7 if raw is None or raw == '' else float(raw)
                except (TypeError, ValueError):
                    s = 0.7
                if s < 0.35:
                    s = 0.35
                if s > 1.5:
                    s = 1.5
                data['pose_strength'] = s
                data['pose_face'] = data.get('pose_face', True) is not False
                desc = data.get('description') if isinstance(data.get('description'), str) else ''
                import pose
                if job is not None:
                    job['message'] = '步骤1/3：准备同图改姿势（锁脸锁衣服）…'
                # 同图重绘：角色参考原图当底图，不做脸/半身裁剪（裁剪会丢掉背景）
                # custom=False so character lock + negative identity lock still apply
                graph = app['builder'].build(
                    desc, data.get('size'), refs, False,
                    data.get('kind') if data.get('kind') in LOCKS else 'character',
                    use_lora=False, lora_strength=None, pose_only=True,
                )
                graph = pose.apply(app, owner, graph, data, data.get('size'))
            else:
                use_lora = data.get('use_lora', False) is True
                if use_lora and not char_lora_ready():
                    use_lora = False
                graph = app['builder'].build(
                    data.get('description'), data.get('size'), refs,
                    data.get('custom') is True,
                    data.get('kind') if data.get('kind') in LOCKS else 'item',
                    use_lora=use_lora, lora_strength=data.get('lora_strength'),
                )
                if data.get('pose'):
                    import pose
                    graph = pose.apply(app, owner, graph, data, data.get('size'))
                    if use_lora and '20' not in graph:
                        apply_char_lora(graph, data.get('lora_strength'))
        if job is not None:
            job['message'] = '步骤2/3：提交到本地模型…'
        client = secrets.token_hex(16)
        ws = await app['http'].ws_connect(BACKEND + '/ws?clientId=' + client, heartbeat=30)
        result = await backend(app, 'POST', '/prompt', json={'prompt': graph, 'client_id': client})
        if not result.get('prompt_id'):
            await ws.close()
            raise BackendRejected('模型未接受任务：' + str(result)[:1600])
        prompt_id = result['prompt_id']
        if job is None:
            job = {'id': request_id, 'owner': owner, 'images': []}
            app['jobs'][request_id] = job
        job['prompt_id'] = prompt_id
        job['_node_types'] = {str(i): n['class_type'] for i, n in graph.items()
                              if isinstance(n, dict) and isinstance(n.get('class_type'), str)}
        job['state'] = 'queued'
        job['message'] = '步骤3/3：已提交，等待采样（进度会显示第几步）'
        job['client'] = client
        telemetry(job, stage='queued', label='等待本地任务', connected=True)
        app['jobs'][prompt_id] = job
        app['submissions'][(owner, request_id)] = {'state': 'submitted', 'id': request_id, 'prompt_id': prompt_id}
        await watch(app, job, ws)
    except Exception as e:
        if job is not None:
            job['state'] = 'error'
            job['message'] = str(e) or '生成失败'
        app['submissions'].pop((owner, request_id), None)


def owner_active_jobs(app, owner):
    """Distinct active jobs for this owner (request_id and prompt_id may alias one job)."""
    seen, out = set(), []
    for j in app['jobs'].values():
        if not isinstance(j, dict) or j.get('owner') != owner:
            continue
        if j.get('state') not in ('queued', 'running', 'cancelling'):
            continue
        key = id(j)
        if key in seen:
            continue
        seen.add(key)
        msg = str(j.get('message') or '')
        label = msg
        if j.get('state') == 'queued' and '排队' not in msg:
            label = '排队等待：' + (msg or '未开始')
        elif j.get('state') == 'running' and not msg:
            label = '进行中'
        out.append({
            'id': j.get('id'),
            'state': j.get('state'),
            'message': msg,
            'label': label,
            'prompt_id': j.get('prompt_id'),
            'created': j.get('created'),
        })
    out.sort(key=lambda x: float(x.get('created') or 0))
    return out


async def my_queue(request):
    app, owner = request.app, request['owner']
    q = owner_active_jobs(app, owner)
    return web.json_response({'queue': q, 'queue_len': len(q)})


async def generate(request):
    import train as _tr; _tr.busy(request.app)
    app, owner = request.app, request['owner']
    data = await request.json()
    require_models()
    request_id = data.get('request_id') or secrets.token_hex(16)
    # Same request_id = idempotent (防误触连点). New request_id = join queue OK.
    previous = submission_start(app, owner, request_id, allow_queue=True)
    if previous:
        return web.json_response({'id': previous, 'queue': owner_active_jobs(app, owner), 'deduped': True})
    refs = owned_references(app, owner, data.get('references', []))
    if data.get('prepared_id') is None and data.get('pose_only') is True:
        if not refs:
            raise ValueError('只改动作需要先添加角色参考照片')
        if not data.get('pose'):
            raise ValueError('只改动作需要先上传动作参考图')
        p = app['poses'].get(data.get('pose')) if isinstance(data.get('pose'), str) else None
        if not p or p['owner'] != owner:
            raise ValueError('动作参考图已失效，请重新上传')
    app['submissions'][(owner, request_id)] = {'state': 'submitted', 'id': request_id}
    app['jobs'][request_id] = {
        'id': request_id, 'owner': owner, 'state': 'running',
        'message': '正在准备任务…', 'images': [], 'prompt_id': None,
        'created': time.time(),
    }
    asyncio.create_task(_generate_worker(app, owner, request_id, dict(data), list(refs)))
    return web.json_response({'id': request_id, 'queue': owner_active_jobs(app, owner)})


async def prepare_materials(request):
    import train as _tr; _tr.busy(request.app)
    app, owner = request.app, request['owner']; data = await request.json()
    require_models()
    request_id = data.get('request_id') or secrets.token_hex(16)
    items = data.get('items')
    if not isinstance(items, list) or not 1 <= len(items) <= 100:
        raise ValueError('整包准备一次需要 1 至 100 张图片')
    resolved = []
    for item in items:
        if not isinstance(item, dict): raise ValueError('素材准备信息格式无效')
        resolved.append({'key': item.get('key'), 'description': item.get('description'),
                         'size': item.get('size'),
                         'references': owned_references(app, owner, item.get('references', []))})
    signature = material_model_signature()
    fingerprint = digest({'kind': 'material_prepare', 'model_signature': signature,
                          'items': resolved})
    graph = app['builder'].prepare(resolved, request_id, signature)
    proofs = {item['key']: material_digest(item['description'], item['size'], item['references'], signature)
              for item in resolved}
    plan = json.loads(graph['4']['inputs']['plan_json'])
    shapes = {item['key']: {'resolution': item['resolution'], 'square': not item['image_keys']}
              for item in plan['items']}
    await material_nodes_ready(app)
    return await submit_graph(app, owner, request_id, graph, metadata={
        'kind': 'material_prepare', 'prepared': [], 'prepare_proofs': proofs, 'prepare_shapes': shapes,
        'model_signature': signature}, fingerprint=fingerprint)

async def submission_status(request):
    app, owner = request.app, request['owner']
    submission = app['submissions'].get((owner, request.match_info['request_id']))
    if not submission: raise web.HTTPNotFound()
    if submission['state'] == 'unknown':
        # Recover by this submission's random client ID; never resubmit work.
        queue = await backend(app, 'GET', '/queue')
        prompts = queue.get('queue_running', []) + queue.get('queue_pending', [])
        matched = next((p for p in prompts if len(p) > 3 and p[3].get('client_id') == submission['client']), None)
        if matched is None:
            history = await backend(app, 'GET', '/history?max_items=100')
            prompts = [record.get('prompt', []) for record in history.values()]
        else: prompts = [matched]
        for prompt in prompts:
            if len(prompt) > 3 and prompt[3].get('client_id') == submission['client']:
                job_id = prompt[1]
                app['jobs'].setdefault(job_id, {'id':job_id, 'owner':owner, 'state':'queued',
                    'message':'已恢复原任务，正在查询结果…', 'images':[], **submission.get('metadata', {})})
                telemetry(app['jobs'][job_id], connected=False)
                submission.update(state='submitted', id=job_id)
                break
    return web.json_response({key:value for key,value in submission.items() if key in ('state', 'id', 'error')})

def owned(request):
    job = request.app['jobs'].get(request.match_info['job'])
    if not job or job['owner'] != request['owner']: raise web.HTTPNotFound()
    return job

async def status(request):
    job = owned(request)
    if not job.get('telemetry'): telemetry(job, connected=False)
    if not job.get('prompt_id') and job.get('state') in ('running', 'queued') and job.get('message', '').startswith('正在'):
        # still preparing identity / submitting — do not query Comfy yet
        return web.json_response({k: job.get(k) for k in ('id','state','message','images','current','total','telemetry') if k in job or k in ('images',)})
    comfy_id = job.get('prompt_id') or job['id']
    history = await backend(request.app,'GET','/history/'+comfy_id)
    if comfy_id in history:
        job.pop('_miss_since', None)
        record = history[comfy_id]; state = record.get('status', {})
        images = []
        for node in record.get('outputs',{}).values():
            for item in node.get('images',[]):
                if item.get('type') == 'output':
                    images.append({'filename':safe_name(item['filename']), 'subfolder':safe_name(item.get('subfolder',''),folder=True), 'type':'output'})
        if state.get('status_str') == 'error':
            interrupted = any(m[0]=='execution_interrupted' for m in state.get('messages',[]))
            errors = [str(m[1].get('exception_message','生成失败')) for m in state.get('messages',[]) if m[0]=='execution_error']
            job.update(state='cancelled' if interrupted else 'error', message='已取消本次生成' if interrupted else (errors[-1] if errors else '生成失败，请检查本地模型状态。'))
        elif state.get('completed') and job.get('kind') == 'material_prepare':
            prepared = record.get('outputs', {}).get('4', {}).get('material_prepared')
            proofs = job.get('prepare_proofs', {})
            shapes = job.get('prepare_shapes', {})
            valid = isinstance(prepared, list) and len(prepared) == len(proofs) and bool(proofs)
            cleaned, keys = [], set()
            if valid:
                for item in prepared:
                    if (not isinstance(item, dict) or item.get('key') not in proofs or item['key'] in keys
                            or not isinstance(item.get('cache_id'), str) or not CACHE_ID.fullmatch(item['cache_id'])
                            or not material_shape_matches(item.get('width'), item.get('height'), shapes.get(item['key']))):
                        valid = False; break
                    keys.add(item['key'])
                    cleaned.append({key: item[key] for key in ('key', 'cache_id', 'width', 'height')})
            if valid:
                permissions = request.app.setdefault('prepared_permissions', {})
                additions = {}
                for item in cleaned:
                    identity = (job['owner'], item['cache_id'])
                    existing = additions.get(identity, permissions.get(identity))
                    shape = shapes[item['key']]
                    if existing and (existing['model_signature'] != job['model_signature']
                            or existing.get('resolution') != shape['resolution']
                            or (existing.get('width'), existing.get('height')) != (item['width'], item['height'])):
                        valid = False; break
                    # Upload names are session-local, while the node cache key
                    # binds actual pixels. A later verified preparation may use
                    # a new upload name for the same pixels and cache record.
                    digests = set(existing['digests']) if existing else set()
                    digests.add(proofs[item['key']])
                    additions[identity] = {'digests': digests, 'model_signature': job['model_signature'],
                                           'resolution': shape['resolution'],
                                           'width': item['width'], 'height': item['height']}
                if valid:
                    permissions.update(additions)
                    job.update(state='done', message='整包准备完成，接下来连续出图', prepared=cleaned)
            if not valid: job.update(state='error', message='整包准备结果不完整，请检查本地准备组件。')
        elif state.get('completed') and images: job.update(state='done', message='图片已生成', images=images)
        elif state.get('completed'): job.update(state='error', message='任务结束，但没有保存图片。')
    elif job['state'] not in ('error','cancelled','done'):
        queue = await backend(request.app,'GET','/queue')
        pending = [q[1] for q in queue.get('queue_pending',[])]; running = [q[1] for q in queue.get('queue_running',[])]
        if comfy_id in pending:
            job.pop('_miss_since', None)
            ahead = len(running) + pending.index(comfy_id)
            job.update(state='queued', message=f'排队中，前面还有 {ahead} 个任务；本任务尚未开始处理')
            telemetry(job, stage='queued', label='排队等待，本任务尚未开始', queue_ahead=ahead)
        elif comfy_id in running:
            job.pop('_miss_since', None)
            job['state']='running'
            if job['telemetry']['stage'] in ('queued', 'verifying'):
                telemetry(job, stage='starting', label='任务已运行，等待详细进度')
        elif job['state']=='cancelling': job.update(state='cancelled',message='已取消本次生成')
        else:
            # Transient gap OK briefly; after ComfyUI restart the id is gone forever.
            import time as _t
            now = _t.time()
            miss_since = job.get('_miss_since')
            if not miss_since:
                job['_miss_since'] = now
                miss_since = now
            waited = now - miss_since
            if waited >= 15:
                job.update(state='error',
                           message='找不到该任务了（多半是本地模型刚重启过，或任务已被清掉）。请重新点生成；不要一直等这条旧任务。')
                telemetry(job, stage='error', label='任务已丢失', connected=False)
            else:
                job['message'] = ('暂未查到任务位置，正在核对结果，请勿重复提交' if waited < 2 else f'暂未查到任务位置，正在核对（已 {int(waited)} 秒），请勿重复提交')
                telemetry(job, stage='verifying', label='正在核实任务状态', connected=False)
    if job['state'] in ('done', 'error', 'cancelled'):
        stage, label = {'done': ('completed', '整包准备完成' if job.get('kind') == 'material_prepare' else '图片已保存'),
                        'error': ('error', '本地任务失败'), 'cancelled': ('cancelled', '任务已取消')}[job['state']]
        telemetry(job, stage=stage, label=label)
        job.pop('connection_note', None)
    _clarify_job_message(job)
    q = owner_active_jobs(request.app, job['owner'])
    payload = {k:v for k,v in job.items() if not k.startswith('_') and k not in ('owner','images','prepare_proofs','prepare_shapes','model_signature')}
    payload['images'] = [f'/api/jobs/{job["id"]}/images/{i}' for i in range(len(job['images']))]
    payload['queue'] = q
    payload['queue_len'] = len(q)
    return web.json_response(payload)

async def cancel(request):
    job = owned(request)
    if job['state'] in ('queued','running','cancelling'):
        result = await backend(request.app,'POST',f'/api/jobs/{job["id"]}/cancel',json={})
        if isinstance(result, dict) and result.get('cancelled') is True:
            job.update(state='cancelling', message='正在取消本次生成…')
        elif isinstance(result, dict) and result.get('cancelled') is False:
            # The job may have completed just before the atomic cancellation.
            # Preserve its output; an unknown ID is not a successful cancel.
            await status(request)
            if job['state'] not in ('done','error','cancelled'):
                raise ValueError('尚未确认取消，请继续查询任务状态。')
        else:
            raise ValueError('尚未确认取消，请继续查询任务状态。')
    return web.json_response({'ok':True})

async def output(request):
    job = owned(request); index = int(request.match_info['index'])
    if index < 0 or index >= len(job['images']): raise web.HTTPNotFound()
    import library, thumbs
    info = job['images'][index]
    # Prefer on-disk file so ?chunk= works through flaky tunnels
    try:
        p = (library.R/'outputs'/(info.get('subfolder') or '')/info.get('filename','')).resolve()
        root = (library.R/'outputs').resolve()
        if p.is_file() and (p == root or root in p.parents):
            return await thumbs.file_response(
                request, p,
                download=request.query.get('download') == '1',
                name=f'generated-{index+1}.png', max_age=0)
    except Exception:
        pass
    # Fallback: full body from Comfy (chunk unsupported on this path)
    if 'chunk' in request.query:
        raise web.HTTPBadRequest(text='chunk needs on-disk image')
    async with request.app['http'].get(BACKEND+'/view?'+urlencode(info)) as response:
        if response.status != 200: raise web.HTTPNotFound()
        headers = {'Cache-Control':'private, no-store', 'X-Content-Type-Options':'nosniff', 'Accept-Ranges':'bytes'}
        if request.query.get('download')=='1': headers['Content-Disposition']=f'attachment; filename="generated-{index+1}.png"'
        body = await response.read()
        # honor Range on buffered body for small parallel pieces
        rng = request.headers.get('Range') or ''
        m = re.match(r'bytes=(\d+)-(\d*)', rng)
        if m:
            total = len(body); start = int(m.group(1)); end = int(m.group(2) or total-1)
            end = min(end, total-1)
            if start > end or start >= total:
                raise web.HTTPRequestRangeNotSatisfiable(headers={'Content-Range': f'bytes */{total}'})
            part = body[start:end+1]
            headers['Content-Range'] = f'bytes {start}-{end}/{total}'
            headers['Content-Length'] = str(len(part))
            return web.Response(body=part, status=206, content_type='image/png', headers=headers)
        return web.Response(body=body, content_type='image/png', headers=headers)

async def output_delete(request):
    job = owned(request); index = int(request.match_info['index'])
    if index < 0 or index >= len(job['images']): raise web.HTTPNotFound()
    import library, thumbs
    info = job['images'][index]
    if info.get('type','output') != 'output': raise web.HTTPNotFound()
    p = (library.R/'outputs'/(info.get('subfolder') or '')/info.get('filename','')).resolve()
    if p.parent != library.HIST.resolve() or not p.is_file(): raise web.HTTPNotFound()
    p.unlink(); thumbs.drop_thumb('history', p.name)
    return web.json_response({'ok':True,'deleted':1})

async def lifetime(app):
    app['http'] = ClientSession(timeout=ClientTimeout(total=90,connect=5))
    yield
    for task in list(app['tasks']): task.cancel()
    await asyncio.gather(*app['tasks'],return_exceptions=True); await app['http'].close()

async def privacy_headers(request, response):
    # Covers FileResponse, ranges, streams, redirects and error responses.
    response.headers['Cache-Control'] = 'private, no-store, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    vary = [v.strip() for v in response.headers.get('Vary', '').split(',') if v.strip()]
    if 'cookie' not in [v.lower() for v in vary]: vary.append('Cookie')
    response.headers['Vary'] = ', '.join(vary)
    if request.path == '/api/logout':
        response.headers['Clear-Site-Data'] = '"cache"'

def create_app():
    app=web.Application(middlewares=[__import__("idle").touch,gzip_middleware,guard],client_max_size=MAX_IMAGE+1024*1024)
    app.on_response_prepare.append(privacy_headers)
    app.update(owners=set(),references={},jobs={},submissions={},prepared_permissions={},tasks=set(),builder=GraphBuilder())
    app.cleanup_ctx.append(lifetime)
    import link_downloads; link_downloads.setup(app)
    import download_links; download_links.setup(app)
    import pack; pack.setup(app, backend)
    import video; video.setup(app)
    import dreamid; dreamid.setup(app)
    import faceswap; faceswap.setup(app)
    import chunkup; chunkup.setup(app)
    import overrides; overrides.setup(app)
    import library; library.setup(app)
    import train; train.setup(app)
    import charpack; charpack.setup(app)
    import char_anim; char_anim.setup(app)
    import motion_preview; motion_preview.setup(app)
    import saved_prompts; saved_prompts.setup(app)
    import pose; pose.setup(app)
    import xunlei; xunlei.setup(app)  # 迅雷 OpenList 已停用：xunlei.py 里不再自动启动 start_alist.sh
    import torrent; torrent.setup(app)
    import histzip; histzip.setup(app)
    import idle; idle.setup(app)
    import lbcache; lbcache.setup(app)
    app.add_routes([web.get('/',index),web.post('/api/logout',studio_logout),web.post('/api/videos/delete',video_library_delete),web.post('/api/login',login),web.post('/login',login_form),web.get('/api/health',health),web.get('/api/lora',lora_status),web.post('/api/upload',upload),web.post('/api/upload-existing',upload_existing),web.post('/api/generate',generate),web.get('/api/queue',my_queue),web.post('/api/prepare-materials',prepare_materials),web.get('/api/submissions/{request_id}',submission_status),web.get('/api/jobs/{job}',status),web.post('/api/jobs/{job}/cancel',cancel),web.get('/api/jobs/{job}/images/{index}',output),web.delete('/api/jobs/{job}/images/{index}',output_delete)])
    return app

if __name__=='__main__': web.run_app(create_app(),host='127.0.0.1',port=6006,access_log=None)
