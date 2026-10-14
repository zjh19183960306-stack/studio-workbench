"""Queue metadata only. No media access in GPU checks or waiting states."""
import json, os, re, subprocess, tempfile, time
from pathlib import Path

ID = re.compile(r'[0-9a-f]{32}\Z')
WAIT = '已排队，需要带卡开机；未读取素材、未开始训练或换脸。'
STEPS = ['排队与 GPU 检查', '传送所选素材', '引擎准备', '执行训练或换脸', '回传结果', '完成']

def atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='.worker-', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
            f.flush(); os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)

def gpu_present():
    try:
        p = subprocess.run(['nvidia-smi', '--query-gpu=uuid', '--format=csv,noheader'],
                           capture_output=True, text=True, timeout=10)
        return p.returncode == 0 and any(x.startswith('GPU-') for x in p.stdout.splitlines())
    except (OSError, subprocess.TimeoutExpired): return False

def update(d, state, message, step=0, **extra):
    d.update(state=state, message=message, step=step, steps=STEPS,
             updated_at=time.time(), **extra)
    return d

def summary(d):
    # Explicit allowlist keeps filenames, material paths and diagnostics private.
    keys = ('id','created_at','updated_at','engine','action','state','step','steps','progress',
            'video_count','face_count','message','inference_started','training_started',
            'environment','results','model_ready','training_iterations')
    return {k: d[k] for k in keys if k in d}
