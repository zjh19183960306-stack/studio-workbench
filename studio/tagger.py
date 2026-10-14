"""帧智能分类：本地 InsightFace(人脸姿态) + 本地 CLIP(零样本) 给每帧打角度/景别/表情标签，全部 CPU 离线。
不保存、不回传画面内容；只写 tags.json（标签+数字）和按类别的硬链接文件夹。"""
import json, os, shutil, threading
from pathlib import Path
import numpy as np

MODELS = Path('/root/autodl-tmp/models/scoring')
_m = {}; _lock = threading.Lock()
IMG = ('.jpg', '.jpeg', '.png', '.webp')

VIEWS = ['正面', '侧前', '侧面', '背面']
FRAMES = ['头肩', '半身', '全身']
PITCH = ['平视', '抬头', '低头']
EXPR = ['平静', '微笑', '大笑', '生气', '难过', '惊讶', '喊叫']
# 训练想要覆盖的组合（缺了就提示）
WANT = {'view': VIEWS, 'framing': FRAMES, 'pitch': PITCH, 'expr': ['平静', '微笑', '大笑', '生气', '惊讶']}

CLIP_TXT = {
    'view': {'正面': 'a photo of a person facing the camera, front view',
             '侧面': 'a photo of a person in side profile view',
             '背面': 'a photo of a person seen from behind, back view, back of the head'},
    'framing': {'头肩': 'a close-up headshot portrait of a person, face and shoulders',
                '半身': 'a medium shot of a person from the waist up',
                '全身': 'a full body shot of a person standing, head to feet visible'},
    'expr': {'平静': 'a face with a neutral calm expression', '微笑': 'a face with a gentle smile',
             '大笑': 'a face laughing with open mouth', '生气': 'an angry face', '难过': 'a sad face',
             '惊讶': 'a surprised face', '喊叫': 'a face shouting or screaming'},
}

def _threads():
    import torch; torch.set_num_threads(16)

def _face():
    with _lock:
        if 'face' not in _m:
            from insightface.app import FaceAnalysis
            a = FaceAnalysis(name='buffalo_l', root=str(MODELS/'insightface'), providers=['CPUExecutionProvider'],
                             allowed_modules=['detection', 'landmark_3d_68'])
            a.prepare(ctx_id=-1, det_size=(640, 640)); _m['face'] = a
        return _m['face']

def _clip():
    with _lock:
        if 'clip' not in _m:
            import torch, open_clip
            _threads()
            local = MODELS/'clip'/'open_clip_pytorch_model.bin'
            if not local.is_file(): raise RuntimeError('本地 CLIP 模型缺失')
            sz = local.stat().st_size
            if _m.get('clip_bad') == sz: raise RuntimeError('本地 CLIP 模型不完整')
            if sz < 600_000_000: _m['clip_bad'] = sz; raise RuntimeError('本地 CLIP 模型还没下完')
            model, _, pre = open_clip.create_model_and_transforms('ViT-B-32', pretrained=str(local))
            model = model.eval().cpu(); tok = open_clip.get_tokenizer('ViT-B-32')
            txt = {}
            with torch.no_grad():
                for k, d in CLIP_TXT.items():
                    e = model.encode_text(tok(list(d.values()))).float(); txt[k] = (list(d), e / e.norm(dim=-1, keepdim=True))
            _m['clip'] = (model, pre, txt)
        return _m['clip']

def _zs(pil, key):
    import torch
    model, pre, txt = _clip(); names, T = txt[key]
    with torch.no_grad():
        e = model.encode_image(pre(pil).unsqueeze(0)).float(); e = e / e.norm(dim=-1, keepdim=True)
        p = (100 * e @ T.T).softmax(-1)[0].numpy()
    i = int(p.argmax()); return names[i], float(p[i]), {n: round(float(x), 3) for n, x in zip(names, p)}

def tag_one(path):
    import cv2
    from PIL import Image
    img = cv2.imread(str(path))
    if img is None: return None
    h, w = img.shape[:2]
    pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    faces = _face().get(img)
    t = {'faces': len(faces)}
    try: vz = _zs(pil, 'view')[0]; fz = _zs(pil, 'framing')[0]; clip_ok = True
    except Exception as e: vz, fz, clip_ok = '?', '半身', False; t['clip_error'] = str(e)[:80]
    if faces:
        f = max(faces, key=lambda x: (x.bbox[2]-x.bbox[0])*(x.bbox[3]-x.bbox[1]))
        x0, y0, x1, y1 = [float(v) for v in f.bbox]
        pitch, yaw, roll = [float(v) for v in (f.pose if getattr(f, 'pose', None) is not None else (0, 0, 0))]
        ay = abs(yaw)
        t['view'] = '正面' if ay < 25 else ('侧前' if ay < 60 else '侧面')
        t['side'] = '' if ay < 25 else ('左' if yaw < 0 else '右')
        t['pitch'] = '平视' if abs(pitch) < 15 else ('抬头' if pitch > 0 else '低头')
        t['yaw'] = round(yaw, 1); t['pitch_deg'] = round(pitch, 1)
        fr = (y1 - y0) / h
        t['framing'] = '头肩' if fr > 0.28 else ('全身' if fr < 0.09 and fz == '全身' else fz if fz != '头肩' else '半身')
        pad = 0.25 * (x1 - x0)
        crop = pil.crop((max(0, x0-pad), max(0, y0-pad), min(w, x1+pad), min(h, y1+pad)))
        t['expr'] = (_zs(crop, 'expr')[0] if clip_ok else '未判断') if (x1 - x0) >= 48 else '看不清'
    else:
        if not clip_ok: vz = '?'
        t['view'] = '背面' if vz == '背面' else ('侧面' if vz == '侧面' else '看不清脸')
        t['pitch'] = '未知'; t['framing'] = fz; t['expr'] = '看不清'
    return t

def tag_folder(folder, state=None, link=True):
    folder = Path(folder)
    files = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMG)
    tags = {}
    if state is not None: state.update(phase='智能分类中', tag_total=len(files), tagged=0)
    for p in files:
        if state is not None and state.get('stop'): break
        try: t = tag_one(p)
        except Exception as e: t = {'error': f'{type(e).__name__}:{str(e)[:120]}'}
        if t: tags[p.name] = t
        if state is not None: state['tagged'] = state.get('tagged', 0) + 1
    (folder/'tags.json').write_text(json.dumps(tags, ensure_ascii=False, indent=0))
    if link: _links(folder, tags)
    return tags

def _links(folder, tags):
    cat = folder/'_分类'; shutil.rmtree(cat, ignore_errors=True)
    for name, t in tags.items():
        if 'error' in t: continue
        for key in ('view', 'framing', 'expr', 'pitch'):
            d = cat/{'view': '角度', 'framing': '景别', 'expr': '表情', 'pitch': '俯仰'}[key]/t.get(key, '未知')
            d.mkdir(parents=True, exist_ok=True)
            try: os.link(folder/name, d/name)
            except OSError: pass

def load(folder):
    try: return json.loads((Path(folder)/'tags.json').read_text())
    except Exception: return None

def coverage(tags):
    cnt = {k: {} for k in ('view', 'framing', 'pitch', 'expr')}; combo = {}
    for t in tags.values():
        if 'error' in t: continue
        for k in cnt: cnt[k][t.get(k, '未知')] = cnt[k].get(t.get(k, '未知'), 0) + 1
        c = t.get('view', '?') + '·' + t.get('framing', '?'); combo[c] = combo.get(c, 0) + 1
    label = {'view': '角度', 'framing': '景别', 'pitch': '俯仰', 'expr': '表情'}
    missing, few = [], []
    for k, want in WANT.items():
        for v in want:
            n = cnt[k].get(v, 0)
            if n == 0: missing.append(f'{label[k]}:{v}')
            elif n < 3: few.append(f'{label[k]}:{v}({n})')
    return {'total': len(tags), 'counts': cnt, 'combos': combo, 'missing': missing, 'few': few}

def balanced(tags, limit=60):
    """按 角度×景别 分桶轮流取，桶内优先表情不同；返回文件名列表。"""
    buckets = {}
    for n, t in sorted(tags.items()):
        if 'error' in t: continue
        buckets.setdefault((t.get('view'), t.get('framing')), []).append(n)
    for k, lst in buckets.items():  # 桶内按表情交错
        by = {}
        for n in lst: by.setdefault(tags[n].get('expr'), []).append(n)
        out = []
        while any(by.values()):
            for e in list(by):
                if by[e]: out.append(by[e].pop(0))
        buckets[k] = out
    pick = []
    while len(pick) < limit and any(buckets.values()):
        for k in list(buckets):
            if buckets[k] and len(pick) < limit: pick.append(buckets[k].pop(0))
    return pick
