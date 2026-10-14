"""照图摆姿势：本地 DWPose 提取全身+手+脸骨架。只在云机本地运行，不联网。
用法: python pose_extract.py 输入图 输出_含表情.png 输出_不含表情.png  → stdout 打印 JSON"""
import sys, os, json
os.environ.setdefault("HF_HUB_OFFLINE", "1")
R = "/root/autodl-tmp/posemodels/comfyui_controlnet_aux/src"
sys.path.insert(0, R)
M = "/root/autodl-tmp/posemodels"
import numpy as np, cv2
from PIL import Image, ImageOps
from custom_controlnet_aux.dwpose import draw_poses
from custom_controlnet_aux.dwpose.wholebody import Wholebody

def main(src, out_face, out_noface):
    img = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
    w, h = img.size; s = min(1.0, 1536 / max(w, h))
    if s < 1: img = img.resize((max(64, round(w*s)), max(64, round(h*s))), Image.LANCZOS)
    arr = np.array(img); H, W = arr.shape[:2]
    model = Wholebody(f"{M}/yolox_l.onnx", f"{M}/dw-ll_ucoco_384.onnx", torchscript_device="cpu")
    poses = Wholebody.format_result(model(arr.copy()))
    people = sum(1 for p in poses if p.body is not None)
    faces = sum(1 for p in poses if p.face is not None)
    hands = sum(1 for p in poses for x in (p.left_hand, p.right_hand) if x is not None)
    for path, face in ((out_face, True), (out_noface, False)):
        canvas = draw_poses(poses, H, W, draw_body=True, draw_hand=True, draw_face=face)
        Image.fromarray(canvas).save(path)
    print(json.dumps({"width": W, "height": H, "people": people, "faces": faces, "hands": hands}))

if __name__ == "__main__":
    main(*sys.argv[1:4])
