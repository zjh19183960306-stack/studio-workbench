"""照图摆姿势：上传动作参考图 → 本地 DWPose 提取骨架（含/不含表情两份）→ 生成时用 Qwen-Image 2.1 Fun ControlNet Union 跟随。
骨架和原图只存在 outputs/_pose（不进单张历史、不进素材库）。"""
import asyncio, json, math, secrets
from pathlib import Path
from aiohttp import web

R = Path('/root/autodl-tmp'); POSE = R/'outputs'/'_pose'
PY = '/root/miniconda3/bin/python'; EXTRACT = Path(__file__).resolve().parent/'pose_extract.py'
CN = 'Qwen-Image-2.1-Fun-Controlnet-Union.safetensors'
MAX = 25*1024*1024
_sem = asyncio.Semaphore(1)

LIB = POSE/'library.json'

def _lib_load():
    try: return json.loads(LIB.read_text())
    except Exception: return {'next': 1, 'items': []}

def _lib_save(d):
    POSE.mkdir(parents=True, exist_ok=True)
    tmp = LIB.with_suffix('.tmp'); tmp.write_text(json.dumps(d, ensure_ascii=False)); tmp.replace(LIB)

def _register(app, owner, item):
    key = secrets.token_urlsafe(18)
    app['poses'][key] = {'owner': owner, 'tok': item['tok'], 'width': item['width'], 'height': item['height'],
                         'people': item.get('people', 1), 'faces': item.get('faces', 0), 'hands': item.get('hands', 0)}
    return {'id': key, 'lib_id': item['id'], 'name': item['name'], 'width': item['width'], 'height': item['height'],
            'people': item.get('people', 1), 'faces': item.get('faces', 0), 'hands': item.get('hands', 0),
            'reused': item.get('_reused', False),
            'preview': f'/api/pose/{key}/skeleton?face=1', 'preview_body': f'/api/pose/{key}/skeleton?face=0'}

def _detect_ext(data):
    return 'png' if data[:8] == b'\x89PNG\r\n\x1a\n' else 'jpg' if data[:3] == b'\xff\xd8\xff' else 'webp' if data[:4] == b'RIFF' and data[8:12] == b'WEBP' else None

async def _extract_bytes(data):
    """Return library item (reuse by sha256)."""
    import hashlib
    h = hashlib.sha256(data).hexdigest()
    lib = _lib_load()
    for it in lib['items']:
        if it.get('hash') == h and (POSE/f"{it['tok']}_face.png").exists():
            it = dict(it); it['_reused'] = True; return it
    ext = _detect_ext(data)
    if not ext: raise ValueError('请使用 PNG、JPG 或 WebP 图片')
    POSE.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_hex(12); src = POSE/f'{tok}_src.{ext}'; src.write_bytes(bytes(data))
    face, body = POSE/f'{tok}_face.png', POSE/f'{tok}_body.png'
    async with _sem:
        p = await asyncio.create_subprocess_exec(PY, str(EXTRACT), str(src), str(face), str(body),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try: out, err = await asyncio.wait_for(p.communicate(), 180)
        except asyncio.TimeoutError:
            p.kill(); raise ValueError('骨架提取超时，请换更小的图再试') from None
    if p.returncode != 0 or not face.exists():
        lines = err.decode('utf-8', 'ignore').strip().splitlines()
        raise ValueError('骨架提取失败：' + (lines[-1][:200] if lines else '未知错误'))
    info = json.loads(out.decode().strip().splitlines()[-1])
    if not info.get('people'): raise ValueError('这张图里没有识别到人，请换一张人物动作清楚的图')
    try: src.unlink()   # 只留骨架，不留原图
    except OSError: pass
    lib = _lib_load(); n = lib.get('next', 1)
    item = {'id': secrets.token_hex(6), 'num': n, 'name': f'{n}号姿势', 'hash': h, 'tok': tok,
            'width': info['width'], 'height': info['height'], 'people': info.get('people', 1),
            'faces': info.get('faces', 0), 'hands': info.get('hands', 0)}
    lib['items'].append(item); lib['next'] = n + 1; _lib_save(lib)
    return item

def _job_start(app, owner, coro_factory):
    jid = secrets.token_hex(8)
    app['pose_jobs'][jid] = {'owner': owner, 'state': 'running'}
    async def run():
        try:
            item = await coro_factory()
            app['pose_jobs'][jid].update(state='done', result=_register(app, owner, item))
        except Exception as e:
            app['pose_jobs'][jid].update(state='error', error=str(e) or '骨架提取失败')
    asyncio.get_event_loop().create_task(run())
    return web.json_response({'pending': jid})

async def upload(request):
    reader = await request.multipart(); part = await reader.next()
    if not part or part.name != 'image': raise ValueError('请选择动作参考图')
    data = bytearray()
    while chunk := await part.read_chunk():
        data.extend(chunk)
        if len(data) > MAX: raise ValueError('动作参考图不得超过 25 MB')
    data = bytes(data)
    return _job_start(request.app, request['owner'], lambda: _extract_bytes(data))

async def from_history(request):
    d = await request.json(); n = str(d.get('history') or '')
    import library
    p = (library.HIST/n).resolve()
    if '/' in n or '\\' in n or p.parent != library.HIST.resolve() or not p.is_file(): raise ValueError('找不到这张历史图片')
    data = p.read_bytes()
    return _job_start(request.app, request['owner'], lambda: _extract_bytes(data))

async def job(request):
    j = request.app['pose_jobs'].get(request.match_info['jid'])
    if not j or j['owner'] != request['owner']: raise web.HTTPNotFound()
    if j['state'] == 'done': return web.json_response({'state': 'done', **j['result']})
    if j['state'] == 'error': return web.json_response({'state': 'error', 'error': j['error']})
    return web.json_response({'state': 'running'})

async def lib_list(request):
    items = [{'id': i['id'], 'name': i['name'], 'num': i['num'], 'width': i['width'], 'height': i['height'],
              'thumb': f"/api/poses/{i['id']}/thumb"} for i in _lib_load()['items']]
    return web.json_response({'items': items})

def _lib_find(lid):
    lib = _lib_load()
    for it in lib['items']:
        if it['id'] == lid: return lib, it
    raise web.HTTPNotFound()

async def lib_thumb(request):
    _, it = _lib_find(request.match_info['lid'])
    f = POSE/f"{it['tok']}_body.png"
    if not f.exists(): f = POSE/f"{it['tok']}_face.png"
    return web.FileResponse(f, headers={'Cache-Control': 'no-store'})

async def lib_use(request):
    _, it = _lib_find(request.match_info['lid'])
    if not (POSE/f"{it['tok']}_face.png").exists(): raise ValueError('这个姿势的骨架文件不见了，请删除后重新提取')
    return web.json_response({'state': 'done', **_register(request.app, request['owner'], it)})

async def lib_rename(request):
    d = await request.json(); name = str(d.get('name') or '').strip()[:40]
    if not name: raise ValueError('名称不能为空')
    lib, it = _lib_find(request.match_info['lid']); it['name'] = name; _lib_save(lib)
    return web.json_response({'ok': True})

async def lib_delete(request):
    lib, it = _lib_find(request.match_info['lid'])
    lib['items'] = [x for x in lib['items'] if x['id'] != it['id']]; _lib_save(lib)
    for k in ('face', 'body'):
        try: (POSE/f"{it['tok']}_{k}.png").unlink()
        except OSError: pass
    return web.json_response({'ok': True})

async def skeleton(request):
    p = request.app['poses'].get(request.match_info['key'])
    if not p or p['owner'] != request['owner']: raise web.HTTPNotFound()
    kind = 'face' if request.query.get('face', '1') == '1' else 'body'
    return web.FileResponse(POSE/f"{p['tok']}_{kind}.png", headers={'Cache-Control': 'no-store'})

def _dwpose_once(img_rgb):
    import os, sys
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    sys.path.insert(0, "/root/autodl-tmp/posemodels/comfyui_controlnet_aux/src")
    import numpy as np
    from custom_controlnet_aux.dwpose.wholebody import Wholebody
    M = "/root/autodl-tmp/posemodels"
    model = Wholebody(f"{M}/yolox_l.onnx", f"{M}/dw-ll_ucoco_384.onnx", torchscript_device="cpu")
    return Wholebody.format_result(model(np.array(img_rgb).copy()))


def identity_face_crop(src: Path, dest: Path) -> Path:
    """Tight face crop — strong face/hair identity, almost no body pose."""
    from PIL import Image, ImageOps
    img = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
    w, h = img.size
    try:
        poses = _dwpose_once(img)
    except Exception:
        poses = None
    if not poses or poses[0].body is None:
        side = min(w, int(h * 0.45), w)
        left = max(0, (w - side) // 2)
        img.crop((left, 0, left + side, min(h, int(side * 1.15)))).save(dest)
        return dest
    kps = poses[0].body.keypoints
    xs, ys = [], []
    for i in (0, 1, 14, 15, 16, 17):
        k = kps[i] if i < len(kps) else None
        if k is not None and float(getattr(k, "score", 1) or 0) > 0.3:
            xs.append(float(k.x)); ys.append(float(k.y))
    face = getattr(poses[0], "face", None)
    if face:
        for k in face:
            if k is not None and float(getattr(k, "score", 1) or 0) > 0.3:
                xs.append(float(k.x)); ys.append(float(k.y))
    if not xs or not ys:
        side = min(w, int(h * 0.45)); left = max(0, (w - side) // 2)
        img.crop((left, 0, left + side, min(h, int(side * 1.15)))).save(dest); return dest
    cx = sum(xs) / len(xs)
    y0, y1 = min(ys), max(ys)
    face_h = max(32.0, y1 - y0)
    box_h = face_h * 2.1
    box_w = face_h * 1.65
    top = max(0, int(y0 - face_h * 0.55))
    bottom = min(h, int(top + box_h))
    left = max(0, int(cx - box_w / 2))
    right = min(w, int(cx + box_w / 2))
    img.crop((left, top, right, bottom)).save(dest)
    return dest


def identity_half_arm_neutral(src: Path, dest: Path) -> Path:
    """Head-to-hip half body with arms/hands painted out — clothes+hair, weak pose."""
    import numpy as np
    from PIL import Image, ImageOps, ImageDraw, ImageFilter
    img = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
    w, h = img.size
    try:
        poses = _dwpose_once(img)
    except Exception:
        poses = None
    top, bottom, left, right = 0, min(h, int(h * 0.72)), 0, w
    if poses and poses[0].body is not None:
        kps = poses[0].body.keypoints
        def xy(i):
            k = kps[i] if i < len(kps) else None
            if k is None or float(getattr(k, "score", 0) or 0) < 0.25:
                return None
            return float(k.x), float(k.y)
        nose = xy(0); neck = xy(1); lhip = xy(11); rhip = xy(12)
        ys = [p[1] for p in (nose, neck, lhip, rhip) if p]
        if ys:
            top = max(0, int(min(ys) - (max(ys) - min(ys)) * 0.35))
            hip_y = max([p[1] for p in (lhip, rhip) if p] or [h * 0.7])
            bottom = min(h, int(hip_y + (hip_y - top) * 0.08))
        crop = img.crop((left, top, right, bottom))
        oy, ox = top, left
        cw, ch = crop.size
        draw = ImageDraw.Draw(crop)
        arms = [(2, 3, 4), (5, 6, 7)]
        for a, b, c in arms:
            pts = []
            for i in (a, b, c):
                p = xy(i)
                if p:
                    pts.append((p[0] - ox, p[1] - oy))
            if len(pts) >= 2:
                draw.line(pts, fill=(0, 0, 0), width=max(28, cw // 12))
            for p in pts:
                r = max(22, cw // 14)
                draw.ellipse((p[0] - r, p[1] - r, p[0] + r, p[1] + r), fill=(0, 0, 0))
        for hand in (getattr(poses[0], "left_hand", None), getattr(poses[0], "right_hand", None)):
            if not hand:
                continue
            hxs, hys = [], []
            for k in hand:
                if k is not None and float(getattr(k, "score", 0) or 0) > 0.2:
                    hxs.append(float(k.x) - ox); hys.append(float(k.y) - oy)
            if hxs:
                pad = max(18, cw // 16)
                draw.ellipse((min(hxs) - pad, min(hys) - pad, max(hxs) + pad, max(hys) + pad), fill=(0, 0, 0))
        arr = np.array(crop)
        mask = (arr[:, :, 0] == 0) & (arr[:, :, 1] == 0) & (arr[:, :, 2] == 0)
        if mask.any():
            bg = arr[~mask]
            fill = tuple(int(x) for x in np.median(bg.reshape(-1, 3), axis=0)) if len(bg) else (180, 180, 180)
            arr[mask] = fill
            crop = Image.fromarray(arr)
            blur = crop.filter(ImageFilter.GaussianBlur(3))
            mimg = Image.fromarray((mask.astype(np.uint8) * 255)).filter(ImageFilter.GaussianBlur(2))
            crop = Image.composite(blur, crop, mimg)
        crop.save(dest)
        return dest
    img.crop((left, top, right, bottom)).save(dest)
    return dest


def prepare_identity_refs(app, owner, ref_filenames):
    """Face crop + arm-neutral half-body + original per ref (identity first, pose via ControlNet)."""
    import hashlib, secrets
    from pathlib import Path as P
    inputs = P("/root/autodl-tmp/inputs")
    out_dir = inputs / "_pose_id"
    out_dir.mkdir(parents=True, exist_ok=True)
    result = []
    for name in ref_filenames:
        src = inputs / name
        if not src.is_file():
            # already a processed _pose_id path
            result.append(name); continue
        try:
            h = hashlib.sha256(src.read_bytes()).hexdigest()[:16]
        except Exception:
            h = secrets.token_hex(8)
        face = out_dir / f"face_{h}.png"
        half = out_dir / f"half_{h}.png"
        try:
            if not face.is_file():
                identity_face_crop(src, face)
            result.append(f"_pose_id/{face.name}")
        except Exception:
            pass
        try:
            if not half.is_file():
                identity_half_arm_neutral(src, half)
            result.append(f"_pose_id/{half.name}")
        except Exception:
            pass
        # Keep original identity ref (do not strip); ControlNet strength handles pose
        result.append(name)
    return result or list(ref_filenames)



def _kp_xy(k, W, H):
    x, y = float(k.x), float(k.y)
    if abs(x) <= 1.5 and abs(y) <= 1.5:
        x, y = x * W, y * H
    return x, y


def _face_from_xy(xy, w, h):
    if xy is None:
        return None
    xs, ys = [], []
    for i in (0, 14, 15, 16, 17):
        p = xy(i)
        if p is not None:
            xs.append(p[0]); ys.append(p[1])
    if not xs:
        return None
    cx = sum(xs) / len(xs)
    cy = sum(ys) / len(ys)
    if len(xs) >= 2:
        rad = max(18.0, (max(ys) - min(ys)) * 0.9, (max(xs) - min(xs)) * 0.75)
    else:
        rad = max(18.0, min(w, h) * 0.06)
    return cx, cy, rad


def limb_change_mask(person: Path, skel: Path, dest: Path, face_dest: Path = None) -> bool:
    """White = full limb volume to replace (source, target, and the sweep between them).
    Black = face, torso core, untouched background. Hard 0/255 — no gaussian feather.
    Writes a tight face-restore mask to face_dest when a face is found."""
    import numpy as np
    from PIL import Image, ImageOps, ImageDraw, ImageFilter
    img = ImageOps.exif_transpose(Image.open(person)).convert("RGB")
    w, h = img.size

    def keypoints(pil, SW, SH):
        try:
            poses = _dwpose_once(pil)
        except Exception:
            poses = None
        if not poses or getattr(poses[0], "body", None) is None:
            return None
        kps = poses[0].body.keypoints
        def xy(i, thr=0.2):
            k = kps[i] if i < len(kps) else None
            if k is None or float(getattr(k, "score", 0) or 0) < thr:
                return None
            x, y = _kp_xy(k, SW, SH)
            return (x * w / max(1.0, float(SW)), y * h / max(1.0, float(SH)))
        return xy

    src_xy = keypoints(img, w, h)
    skel_img = ImageOps.exif_transpose(Image.open(skel)).convert("RGB")
    sw, sh = skel_img.size
    tgt_xy = keypoints(skel_img, sw, sh)

    def paint(radius):
        mask = Image.new("L", (w, h), 0)
        draw = ImageDraw.Draw(mask)
        r = float(radius)
        arms = ((1, 2), (2, 3), (3, 4), (1, 5), (5, 6), (6, 7))
        legs = ((8, 9), (9, 10), (11, 12), (12, 13))

        def blob(p, rad):
            draw.ellipse((p[0] - rad, p[1] - rad, p[0] + rad, p[1] + rad), fill=255)

        def segment(xy, a, b, rad):
            if xy is None:
                return None
            pa, pb = xy(a), xy(b)
            if pa is None or pb is None:
                return None
            draw.line((pa, pb), fill=255, width=max(1, int(rad * 2)))
            blob(pa, rad)
            blob(pb, rad)
            return pa, pb

        for a, b in arms:
            # capsules only: erase the old limb and draw the new one.
            # a filled sweep repaints the shirt between them.
            segment(src_xy, a, b, r)
            segment(tgt_xy, a, b, r * 0.85)
        skel_r = skel_img.resize((w, h), Image.BILINEAR)
        bright = (np.asarray(skel_r).max(axis=2) > 28).astype(np.uint8) * 255
        bimg = Image.fromarray(bright, "L")
        k = max(9, (int(r) // 2) | 1)
        if k % 2 == 0:
            k += 1
        bimg = bimg.filter(ImageFilter.MaxFilter(min(k, 31)))
        barr = np.array(bimg, copy=True)
        barr[int(h * 0.50):, :] = 0
        barr[:, int(w * 0.44):int(w * 0.56)] = 0
        mask = Image.fromarray(np.maximum(np.asarray(mask), barr), "L")
        draw = ImageDraw.Draw(mask)
        face = _face_from_xy(src_xy, w, h)
        if face is not None:
            cx, cy, rad = face
            rad = max(rad, min(w, h) * 0.09)
            top = max(0.0, cy - rad * 1.9)
            # face only — a tall ellipse was locking the short sleeves in place
            draw.ellipse((cx - rad * 1.2, top, cx + rad * 1.2, cy + rad * 1.05), fill=0)
            draw.rectangle((int(w * 0.46), int(cy), int(w * 0.54), int(h * 0.42)), fill=0)
        else:
            draw.ellipse((w * 0.32, h * 0.0, w * 0.68, h * 0.32), fill=0)
        draw.rectangle((int(w * 0.47), int(h * 0.40), int(w * 0.53), int(h * 0.56)), fill=0)
        arr = np.where(np.asarray(mask) > 48, 255, 0).astype(np.uint8)
        return arr, face

    chosen = None
    for radius in (max(40, int(min(w, h) / 14)), max(28, int(min(w, h) / 20))):
        arr, face = paint(radius)
        frac = float((arr > 40).mean())
        chosen = (arr, face, frac, radius)
        if 0.03 <= frac <= 0.55:
            break
    arr, face, frac, radius = chosen
    logp = Path("/root/autodl-tmp/_selftest/mask_frac.txt")
    if frac < 0.02 or frac > 0.62:
        logp.write_text("reject frac %.3f radius %s\n" % (frac, radius))
        return False
    hard = Image.fromarray(arr, "L")
    Image.merge("RGB", (hard, hard, hard)).save(dest)
    if face_dest is not None and face is not None:
        fm = Image.new("L", (w, h), 0)
        fd = ImageDraw.Draw(fm)
        cx, cy, rad = face
        fd.ellipse((cx - rad * 0.95, cy - rad * 1.15, cx + rad * 0.95, cy + rad * 0.85), fill=255)
        Image.merge("RGB", (fm, fm, fm)).save(face_dest)
    logp.write_text("ok frac %.3f radius %s size %dx%d\n" % (frac, radius, w, h))
    return True



def _skel_head_norm(skel_path: Path):
    """Head center as fractions of the skeleton canvas."""
    import numpy as np
    from PIL import Image, ImageOps
    im = ImageOps.exif_transpose(Image.open(skel_path)).convert("RGB")
    arr = np.asarray(im)
    bright = arr.max(axis=2) > 24
    ys, xs = np.where(bright)
    if len(ys) < 30:
        return None
    top = int(ys.min())
    span = max(1, int(ys.max()) - top)
    band = ys <= top + max(6, int(span * 0.14))
    if int(band.sum()) < 8:
        return None
    h, w = arr.shape[:2]
    return float(xs[band].mean()) / float(w), float(ys[band].mean()) / float(h)


def _source_face(img):
    """(cx, cy, rad) in pixels from DWPose, or None."""
    w, h = img.size
    try:
        poses = _dwpose_once(img)
    except Exception:
        return None
    if not poses or getattr(poses[0], "body", None) is None:
        return None
    kps = poses[0].body.keypoints

    def xy(i, thr=0.25):
        k = kps[i] if i < len(kps) else None
        if k is None or float(getattr(k, "score", 0) or 0) < thr:
            return None
        return _kp_xy(k, w, h)

    return _face_from_xy(xy, w, h)



def identity_armless_full(src: Path, dest: Path) -> Path:
    """Full frame, arms and hands painted to the border color. Face, torso, legs stay."""
    import numpy as np
    from PIL import Image, ImageOps, ImageDraw
    img = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
    w, h = img.size
    arr = np.array(img)
    border = np.concatenate([arr[0], arr[-1], arr[:, 0], arr[:, -1]], axis=0)
    fill = tuple(int(x) for x in np.median(border, axis=0))
    try:
        poses = _dwpose_once(img)
    except Exception:
        poses = None
    if not poses or getattr(poses[0], "body", None) is None:
        img.save(dest)
        return dest
    mask = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(mask)
    kps = poses[0].body.keypoints

    def xy(i, thr=0.22):
        k = kps[i] if i < len(kps) else None
        if k is None or float(getattr(k, "score", 0) or 0) < thr:
            return None
        return _kp_xy(k, w, h)

    thick = max(36, int(min(w, h) / 11))
    for a, b in ((2, 3), (3, 4), (5, 6), (6, 7)):
        pa, pb = xy(a), xy(b)
        if pa and pb:
            draw.line((pa, pb), fill=255, width=thick)
            r = thick // 2
            for pt in (pa, pb):
                draw.ellipse((pt[0] - r, pt[1] - r, pt[0] + r, pt[1] + r), fill=255)
    for hand in (getattr(poses[0], "left_hand", None), getattr(poses[0], "right_hand", None)):
        if not hand:
            continue
        xs, ys = [], []
        for k in hand:
            if k is not None and float(getattr(k, "score", 0) or 0) > 0.2:
                x, y = _kp_xy(k, w, h)
                xs.append(x); ys.append(y)
        if xs:
            pad = thick * 0.6
            draw.ellipse((min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad), fill=255)
    m = np.asarray(mask) > 0
    arr[m] = fill
    Image.fromarray(arr).save(dest)
    return dest


def build_restore_plate(person: Path, skel: Path, plate_path: Path, mask_path: Path) -> bool:
    return False
    """Pass B plate: shifted face + narrow torso/legs. Never the old arms.

    Black everywhere else. Mask is white only on pasted pixels. The face is
    moved onto the skeleton head so the original head is not left in place.
    """
    import numpy as np
    from PIL import Image, ImageOps
    img = ImageOps.exif_transpose(Image.open(person)).convert("RGB")
    w, h = img.size
    src = np.asarray(img)
    plate = np.zeros_like(src)
    mask = np.zeros((h, w), np.uint8)
    face = _source_face(img)
    y0 = int(h * 0.22)
    if face is not None:
        _cx, cy, rad = face
        y0 = int(min(h - 1, max(cy + rad * 0.85, h * 0.16)))
    y1 = int(h * 0.96)
    x0 = int(w * 0.43)
    x1 = int(w * 0.57)
    if y1 > y0 + 8 and x1 > x0 + 4:
        plate[y0:y1, x0:x1] = src[y0:y1, x0:x1]
        mask[y0:y1, x0:x1] = 255
    head = _skel_head_norm(skel)
    if face is not None and head is not None:
        cx, cy, rad = face
        rad = float(max(rad, min(w, h) * 0.055))
        rad = float(min(rad, min(w, h) * 0.13))
        tx = head[0] * w
        ty = head[1] * h
        yy, xx = np.ogrid[:h, :w]
        dest = ((xx - tx) ** 2) / ((rad * 1.15) ** 2) + ((yy - ty) ** 2) / ((rad * 1.35) ** 2) <= 1.0
        sy = np.clip(np.rint(yy - (ty - cy)).astype(np.int32), 0, h - 1)
        sx = np.clip(np.rint(xx - (tx - cx)).astype(np.int32), 0, w - 1)
        sy_b = np.broadcast_to(sy, (h, w))
        sx_b = np.broadcast_to(sx, (h, w))
        src_ok = ((sx_b - cx) ** 2) / ((rad * 1.15) ** 2) + ((sy_b - cy) ** 2) / ((rad * 1.35) ** 2) <= 1.0
        take = dest & src_ok
        plate[take] = src[sy_b[take], sx_b[take]]
        mask[take] = 255
    frac = float((mask > 0).mean())
    logp = Path("/root/autodl-tmp/_selftest/redraw_params.txt")
    if frac < 0.008 or frac > 0.30:
        try:
            logp.write_text("restore_reject frac %.4f\n" % frac)
        except Exception:
            pass
        return False
    Image.fromarray(plate).save(plate_path)
    mimg = Image.fromarray(mask, "L")
    Image.merge("RGB", (mimg, mimg, mimg)).save(mask_path)
    return True


def apply(app, owner, graph, data, size):
    """照图摆姿势。

    重要：TextEncodeQwenImage21 的第 3 路输出 ([4,2]) 是按参考图尺寸造的【全零空 latent】，
    不是底图 VAE 编码。对它做 denoise<1 的 img2img = 从零噪声半截去噪 = 炸花噪声图。

    pose_only（同图重绘）:
      VAEEncode(人物参考) + 中等 denoise(~0.58) + ControlNet(~1.05)。
      保脸保衣优先；姿势大概对即可。文本条件用脸裁剪+无臂全身，避免锁死旧手臂。
    有参考图但非 pose_only:
      官方 reference_edit：空 latent + denoise=1.0 + reference_latents 条件。
    无参考图:
      EmptyQwenImage21Latent + denoise=1.0 + ControlNet。
    """
    key = data.get('pose')
    p = app['poses'].get(key) if isinstance(key, str) else None
    if not p or p['owner'] != owner: raise ValueError('动作参考图已失效，请重新上传')
    s = data.get('pose_strength', 0.7)
    if type(s) not in (int, float) or not 0 <= s <= 3: raise ValueError("动作跟随强度需在 0–3 之间")
    kind = 'face' if data.get('pose_face', True) is not False else 'body'
    if not (POSE/f"{p['tok']}_{kind}.png").exists(): raise ValueError('动作骨架文件不见了，请重新上传动作参考图')
    ks = graph.get('5', {})
    _unet = graph.get('1', {}).get('class_type')
    if (ks.get('class_type') != 'KSampler'
            or _unet not in ('UNETLoader', 'UnetLoaderGGUF')
            or graph.get('3', {}).get('class_type') != 'VAELoader'):
        raise ValueError('当前任务图不支持照图摆姿势')
    # idempotent re-apply
    for k in ('30', '31', '32', '33', '34', '35', '36', '37', '38', '39', '40', '41', '42', '43', '44'):
        graph.pop(k, None)
    graph['30'] = {'class_type': 'ModelPatchLoader', 'inputs': {'name': CN}}
    graph['31'] = {'class_type': 'LoadImage', 'inputs': {'image': f"_pose/{p['tok']}_{kind}.png [output]"}}
    src_model = ks['inputs'].get('model', ['1', 0])
    pose_only = data.get('pose_only') is True
    # Identity/clothes first; pose approximate. UI slider removed.
    if pose_only:
        s = 0.45
    cn_s = float(s)
    if pose_only:
        cn_s = 1.05
    graph['32'] = {'class_type': 'ZImageFunControlnet', 'inputs': {
        'model': src_model, 'model_patch': ['30', 0], 'vae': ['3', 0],
        'strength': float(cn_s), 'image': ['31', 0]}}
    ks['inputs']['model'] = ['32', 0]
    # first character LoadImage (skip pose skeleton node 31)
    ref_node = None
    for k in sorted((x for x in graph if str(x).isdigit()), key=lambda x: int(x)):
        if int(k) < 8:
            continue
        node = graph[k]
        if node.get('class_type') != 'LoadImage':
            continue
        img = str(node.get('inputs', {}).get('image', ''))
        if '_pose/' in img or k in ('31',):
            continue
        ref_node = str(k)
        break

    if pose_only:
        if ref_node is None:
            raise ValueError('同图重绘需要至少一张人物参考图')
        # Identity-first: VAEEncode person photo as img2img base (keep face/clothes/bg).
        # Full redraw (denoise=1 empty latent) was inventing new outfits.
        res = graph.get('4', {}).get('inputs', {}).get('resolution')
        if type(res) not in (int, float) or res <= 0:
            res = size if type(size) in (int, float) and size > 0 else 1024
        res = int(res)
        megapixels = max(0.25, min(16.0, (res * res) / 1_000_000.0))
        graph['34'] = {'class_type': 'ImageScaleToTotalPixels', 'inputs': {
            'image': [ref_node, 0], 'upscale_method': 'lanczos',
            'megapixels': float(megapixels), 'resolution_steps': 32,
        }}
        graph['35'] = {'class_type': 'VAEEncode', 'inputs': {
            'pixels': ['34', 0], 'vae': ['3', 0],
        }}
        ks['inputs']['latent_image'] = ['35', 0]
        raw_d = data.get('pose_denoise')
        if type(raw_d) in (int, float):
            denoise = float(raw_d)
        else:
            # 0.62: keep outfit, still let ControlNet bend arms
            denoise = 0.62
        denoise = max(0.52, min(0.70, denoise))
        ks['inputs']['denoise'] = denoise
        graph['32']['inputs']['strength'] = 1.15
        src_path = None
        base_img = graph.get(ref_node, {}).get('inputs', {}).get('image')
        inputs_dir = Path('/root/autodl-tmp/inputs')
        out_dir = inputs_dir / '_pose_id'
        out_dir.mkdir(parents=True, exist_ok=True)
        if isinstance(base_img, str) and base_img:
            cand = inputs_dir / base_img
            if cand.is_file():
                src_path = cand
        if graph.get('4', {}).get('class_type') == 'TextEncodeQwenImage21':
            graph['4']['inputs']['vae'] = ['3', 0]
            neg = str(graph['4']['inputs'].get('negative_prompt') or '')
            extra = ('，换成另一套衣服，改变服装颜色，改变衣服款式，换装，不同衣服，'
                     '第二个人，第二颗头，多余手臂，重影手臂，鬼影，'
                     '握拳，攥拳，拳头，closed fist，clenched fist，缺手指')
            if '换成另一套衣服' not in neg:
                graph['4']['inputs']['negative_prompt'] = neg + extra
            if src_path is not None:
                try:
                    import hashlib as _hl
                    hh = _hl.sha256(src_path.read_bytes()).hexdigest()[:16]
                    face = out_dir / f'face_{hh}.png'
                    if not face.is_file():
                        identity_face_crop(src_path, face)
                    graph['37'] = {'class_type': 'LoadImage', 'inputs': {'image': f'_pose_id/{face.name}'}}
                    wired = False
                    for ik in list(graph['4']['inputs']):
                        if isinstance(ik, str) and ik.startswith('images.image_'):
                            graph['4']['inputs'][ik] = ['37', 0]
                            wired = True
                    if not wired:
                        graph['4']['inputs']['images.image_1'] = ['37', 0]
                    arm = out_dir / f'armless_{hh}.png'
                    if not arm.is_file():
                        identity_armless_full(src_path, arm)
                    if arm.is_file() and arm.stat().st_size > 80:
                        graph['36'] = {'class_type': 'LoadImage', 'inputs': {'image': f'_pose_id/{arm.name}'}}
                        graph['4']['inputs']['images.image_2'] = ['36', 0]
                except Exception:
                    import traceback
                    try:
                        Path('/root/autodl-tmp/_selftest/mask_err.txt').write_text(traceback.format_exc()[-2000:])
                    except Exception:
                        pass
        restore_ok = False
        if (src_path is not None
                and graph.get('6', {}).get('class_type') == 'VAEDecode'
                and graph.get('7', {}).get('class_type') == 'SaveImage'):
            try:
                import hashlib as _hl
                skel = POSE / f"{p['tok']}_{kind}.png"
                hh = _hl.sha256(src_path.read_bytes()).hexdigest()[:12]
                sh = _hl.sha256(skel.read_bytes()).hexdigest()[:8] if skel.is_file() else 'noskel'
                plate = out_dir / f'redraw_{hh}_{sh}.png'
                rmask = out_dir / f'redrawm_{hh}_{sh}.png'
                if skel.is_file() and build_restore_plate(src_path, skel, plate, rmask):
                    graph['42'] = {'class_type': 'LoadImage', 'inputs': {'image': f'_pose_id/{plate.name}'}}
                    graph['43'] = {'class_type': 'LoadImage', 'inputs': {'image': f'_pose_id/{rmask.name}'}}
                    graph['39'] = {'class_type': 'ImageToMask', 'inputs': {'image': ['43', 0], 'channel': 'red'}}
                    graph['44'] = {'class_type': 'FaceRestoreComposite', 'inputs': {
                        'generated': ['6', 0], 'original': ['42', 0], 'face_mask': ['39', 0]}}
                    graph['7']['inputs']['images'] = ['44', 0]
                    restore_ok = True
            except Exception:
                import traceback
                try:
                    Path('/root/autodl-tmp/_selftest/mask_err.txt').write_text(traceback.format_exc()[-2000:])
                except Exception:
                    pass
        try:
            Path('/root/autodl-tmp/_selftest/redraw_params.txt').write_text(
                "mode=img2img_keep_clothes denoise=%.3f cn=%.2f restore=%s armless=1\n"
                % (float(denoise), float(graph['32']['inputs']['strength']), int(restore_ok)))
        except Exception:
            pass
    elif ref_node is not None and graph.get('4', {}).get('class_type') == 'TextEncodeQwenImage21':
        # official Qwen edit: empty latent sized by encoder + full denoise
        ks['inputs']['latent_image'] = ['4', 2]
        ks['inputs']['denoise'] = 1.0
    else:
        ratio = p['width'] / max(1, p['height'])
        w = max(256, round(math.sqrt(size * size * ratio) / 32) * 32)
        h = max(256, round(math.sqrt(size * size / ratio) / 32) * 32)
        graph['33'] = {'class_type': 'EmptyQwenImage21Latent', 'inputs': {'width': w, 'height': h, 'batch_size': 1}}
        ks['inputs']['latent_image'] = ['33', 0]
        ks['inputs']['denoise'] = 1.0
    return graph

def setup(app):
    app['poses'] = {}; app['pose_jobs'] = {}
    app.add_routes([web.post('/api/pose', upload), web.post('/api/pose/from-history', from_history),
                    web.get('/api/pose/job/{jid}', job), web.get('/api/pose/{key}/skeleton', skeleton),
                    web.get('/api/poses', lib_list), web.get('/api/poses/{lid}/thumb', lib_thumb),
                    web.post('/api/poses/{lid}/use', lib_use), web.post('/api/poses/{lid}/rename', lib_rename),
                    web.delete('/api/poses/{lid}', lib_delete)])
