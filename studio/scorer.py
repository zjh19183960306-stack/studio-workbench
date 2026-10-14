"""本地相似度打分 + 自动质检 + 问题本。全部离线推理；不记录提示词原文。"""
import json, os, sqlite3, threading, time, hashlib
from pathlib import Path
import numpy as np
os.environ.setdefault("HF_HUB_OFFLINE", "1")

R = Path("/root/autodl-tmp"); QA = R/"qa"; MODELS = R/"models"/"scoring"
DB = QA/"problem_book.sqlite"; CFG = QA/"config.json"
DEFAULT_CFG = {"threshold": 45, "retries": 2, "train_target": 30}
_lock = threading.Lock(); _m = {}

def config():
    try: c = json.loads(CFG.read_text())
    except Exception: c = {}
    out = dict(DEFAULT_CFG)
    for k in out:
        if isinstance(c.get(k), (int, float)) and not isinstance(c.get(k), bool): out[k] = c[k]
    return out

def save_config(d):
    c = config()
    if "threshold" in d: c["threshold"] = max(0, min(100, float(d["threshold"])))
    if "retries" in d: c["retries"] = int(max(0, min(5, int(d["retries"]))))
    if "train_target" in d: c["train_target"] = int(max(5, min(500, int(d["train_target"]))))
    QA.mkdir(parents=True, exist_ok=True); CFG.write_text(json.dumps(c)); return c

def _providers():
    # ComfyUI 占满 GPU，打分固定走 CPU，避免抢显存；也不依赖 CUDA 13 库。
    return ["CPUExecutionProvider"]

def _face():
    if "face" in _m: return _m["face"]
    if _m.get("face_err"): raise RuntimeError(_m["face_err"])
    try:
        from insightface.app import FaceAnalysis
        app = FaceAnalysis(name="buffalo_l", root=str(MODELS/"insightface"),
                           providers=_providers(),
                           allowed_modules=["detection", "recognition", "genderage"])
        app.prepare(ctx_id=-1, det_size=(640, 640))
        _m["face"] = app
        return app
    except Exception as e:
        _m["face_err"] = f"face_model:{type(e).__name__}:{str(e)[:180]}"
        raise RuntimeError(_m["face_err"]) from e

def _clip():
    """只用本地 CLIP 权重；缺失/损坏就报错（带缓存），绝不联网。"""
    if "clip" in _m: return _m["clip"]
    local = MODELS/"clip"/"open_clip_pytorch_model.bin"
    sz = local.stat().st_size if local.is_file() else 0
    if _m.get("clip_err") and _m.get("clip_err_size") == sz: raise RuntimeError(_m["clip_err"])
    try:
        import torch, open_clip
        if sz < 500_000_000: raise FileNotFoundError(f"local clip incomplete ({sz} bytes)")
        model, _, pre = open_clip.create_model_and_transforms("ViT-B-32", pretrained=str(local))
    except Exception as e:
        _m["clip_err"] = f"clip:{type(e).__name__}:{str(e)[:150]}"; _m["clip_err_size"] = sz
        raise RuntimeError(_m["clip_err"]) from e
    model = model.eval().cpu()
    _m["clip"] = (model, pre, "cpu")
    return _m["clip"]

def _clip_try(path):
    try: return _clip_emb(path), None
    except Exception as e: return None, str(e)[:150]

def _clip_emb(path):
    import torch
    from PIL import Image
    model, pre, dev = _clip()
    with Image.open(path) as im, torch.no_grad():
        x = pre(im.convert("RGB")).unsqueeze(0)
        e = model.encode_image(x).float(); e = e / e.norm(dim=-1, keepdim=True)
    return e[0].cpu().numpy()

def _faces(path):
    import cv2
    img = cv2.imread(str(path))
    if img is None: return [], None
    faces = _face().get(img)
    if not faces:
        # 近景大脸检测不到时，四周补黑边再检一次（只做本地推理）
        h, w = img.shape[:2]
        pad = cv2.copyMakeBorder(img, h//2, h//2, w//2, w//2, cv2.BORDER_CONSTANT)
        faces = _face().get(pad)
    return faces, img

def ref_multi(refs):
    """多张参考照：把所有能检出人脸的身份照平均成一个身份向量；返回可传给 score 的首张路径。"""
    embs, ages, first = [], [], None
    for p in refs:
        try:
            fs, _ = _faces(p)
        except Exception: continue
        if not fs: continue
        f = max(fs, key=lambda f: (f.bbox[2]-f.bbox[0])*(f.bbox[3]-f.bbox[1]))
        embs.append(f.normed_embedding); first = first or p
        if getattr(f, "age", None) is not None: ages.append(float(f.age))
    if not embs: return None, 0
    e = np.mean(embs, axis=0); e = e / np.linalg.norm(e)
    k = (str(first), Path(first).stat().st_mtime_ns)
    clip, _ce = _clip_try(first)
    _refcache[k] = {"emb": e, "age": (sum(ages)/len(ages)) if ages else None, "clip": clip, "face_err": None}
    return first, len(embs)

def _sharp(img):
    import cv2
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY); h, w = g.shape; s = 720 / max(h, w)
    if s < 1: g = cv2.resize(g, (int(w*s), int(h*s)))
    return float(cv2.Laplacian(g, cv2.CV_64F).var())

_refcache = {}
def _ref_info(ref):
    k = (str(ref), Path(ref).stat().st_mtime_ns)
    if k not in _refcache:
        clip, _ce = _clip_try(ref)
        emb = age = None
        try:
            faces, _ = _faces(ref)
            f = max(faces, key=lambda f: (f.bbox[2]-f.bbox[0])*(f.bbox[3]-f.bbox[1])) if faces else None
            if f is not None:
                emb = f.normed_embedding
                age = float(f.age) if getattr(f, "age", None) is not None else None
        except Exception as e:
            _refcache[k] = {"emb": None, "age": None, "clip": clip, "face_err": str(e)[:120]}
            return _refcache[k]
        _refcache[k] = {"emb": emb, "age": age, "clip": clip, "face_err": None}
    return _refcache[k]

def score(gen, ref, kind, category, batch_clips):
    """返回 {score 0-100, method, flags[], clip(np), metrics{}}。"""
    with _lock:
        import cv2
        img = cv2.imread(str(gen))
        flags = []; m = {}
        if img is None:
            return {"score": 0, "method": "error", "flags": ["unreadable"], "clip": None, "metrics": {}}
        m["sharpness"] = round(_sharp(img), 1)
        if m["sharpness"] < 60: flags.append("blurry")
        r = _ref_info(ref)
        ce, cerr = _clip_try(gen)
        back = category in ("back",) or "背" in str(category)
        s = None; method = "none"
        if ce is not None and r.get("clip") is not None:
            clip_cos = float(np.dot(ce, r["clip"])); m["clip_cos"] = round(clip_cos, 4)
            s = max(0., min(1., (clip_cos - 0.5) / 0.45)) * 100; method = "clip"
        elif cerr: m["clip_err"] = cerr
        if r.get("face_err"):
            m["face_err"] = r["face_err"]
        if kind == "character" and r["emb"] is not None:
            try:
                faces, _ = _faces(gen)
                if faces:
                    f = max(faces, key=lambda f: (f.bbox[2]-f.bbox[0])*(f.bbox[3]-f.bbox[1]))
                    cos = float(np.dot(f.normed_embedding, r["emb"])); m["face_cos"] = round(cos, 4)
                    s = max(0., min(1., (cos - 0.15) / 0.55)) * 100; method = "face"
                    if len(faces) > 1: flags.append("multiple_faces")
                    if r["age"] is not None and getattr(f, "age", None) is not None:
                        m["age_drift"] = round(float(f.age) - r["age"], 1)
                        if abs(m["age_drift"]) > 12: flags.append("age_drift")
                elif not back and category not in ("full_body",):
                    flags.append("face_missing")
            except Exception as e:
                m["face_err"] = str(e)[:120]
        elif kind == "character" and r["emb"] is None and not r.get("face_err"):
            # ref had no face; stay on clip
            pass
        if ce is not None and batch_clips:
            dmax = max(float(np.dot(ce, c)) for c in batch_clips); m["dup_cos"] = round(dmax, 4)
            if dmax > 0.97: flags.append("near_duplicate")
        return {"score": None if s is None else round(s, 1), "method": method, "flags": flags, "clip": ce, "metrics": m}

def _db():
    QA.mkdir(parents=True, exist_ok=True); c = sqlite3.connect(DB)
    c.execute("create table if not exists runs(ts real, batch text, n int, label text, category text, kind text, prompt_src text,"
              " prompt_hash text, variant int, seed int, size int, attempt int, score real, method text, flags text, accepted int)")
    return c

def log(**k):
    with _db() as c:
        c.execute("insert into runs values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (time.time(), k["batch"], k["n"], k["label"], k["category"], k["kind"],
                  k["prompt_src"], k["prompt_hash"], k["variant"], k.get("seed"), k["size"], k["attempt"], k["score"], k["method"],
                  json.dumps(k["flags"]), int(k["accepted"])))

def best_variant(label, kind, n_variants):
    try:
        with _db() as c:
            rows = c.execute("select variant, avg(score), count(*) from runs where label=? and kind=? group by variant", (label, kind)).fetchall()
    except Exception: return 0
    good = [(v, a) for v, a, cnt in rows if v is not None and v < n_variants and cnt >= 1]
    return max(good, key=lambda x: x[1])[0] if good else 0

def stats(threshold):
    with _db() as c:
        per = c.execute("select label, kind, count(*), sum(score<?), avg(score) from runs group by label, kind order by sum(score<?)*1.0/count(*) desc limit 40",
                        (threshold, threshold)).fetchall()
        flags = {}
        for (f,) in c.execute("select flags from runs"):
            for x in json.loads(f or "[]"): flags[x] = flags.get(x, 0) + 1
        tot = c.execute("select count(*), sum(accepted) from runs").fetchone()
    return {"shots": [{"label": l, "kind": k, "runs": n, "fails": int(fl or 0), "avg": round(a or 0, 1)} for l, k, n, fl, a in per],
            "flags": flags, "runs": tot[0] or 0, "accepted": int(tot[1] or 0)}

def prompt_hash(text):
    return hashlib.sha256(text.encode()).hexdigest()[:12]

def readiness():
    """供接口探测：模型文件与能否加载（不读用户图）。"""
    face_dir = MODELS/"insightface"/"models"/"buffalo_l"
    need = ["det_10g.onnx", "w600k_r50.onnx", "genderage.onnx"]
    files = {n: (face_dir/n).is_file() and (face_dir/n).stat().st_size for n in need}
    clip = MODELS/"clip"/"open_clip_pytorch_model.bin"
    out = {"threshold": config()["threshold"], "files": files, "clip_bytes": clip.stat().st_size if clip.is_file() else 0,
           "face_ok": False, "clip_ok": False, "face_err": None, "clip_err": None}
    try:
        _face(); out["face_ok"] = True
    except Exception as e:
        out["face_err"] = str(e)[:200]
    try:
        _clip(); out["clip_ok"] = True
    except Exception as e:
        out["clip_err"] = str(e)[:200]
    return out
