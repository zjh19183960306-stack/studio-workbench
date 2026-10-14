#!/bin/bash
# 开机自启：先起工作台+公网隧道（秒开），有显卡才起 ComfyUI。可重复执行。
exec 9>/tmp/autostart2.lock; flock -n 9 || exit 0
export PATH=/root/miniconda3/bin:$PATH HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 NO_TOKEN=0
R=/root/autodl-tmp; L=$R/autostart.log
log(){ echo "$(date "+%F %T") $*" >> $L; }
log "autostart 开始"
mkdir -p $R/inputs $R/outputs
if ! curl -s -m 2 -o /dev/null 127.0.0.1:6006/api/health; then
  cd $R/studio && setsid nohup python server.py > $R/studio.log 2>&1 < /dev/null 9>&- &
  log "启动 studio"
fi
pgrep -f "autodl-tmp/tunnel_loop.sh" >/dev/null || { setsid nohup $R/tunnel_loop.sh >/dev/null 2>&1 < /dev/null 9>&- & log "启动 隧道"; }
[ -x $R/idle_shutdown.sh ] && { pgrep -f "autodl-tmp/idle_shutdown.sh" >/dev/null || { setsid nohup $R/idle_shutdown.sh >/dev/null 2>&1 < /dev/null 9>&- & log "启动 空闲关机"; }; }
pgrep -f "autodl-tmp/watch_comfy.sh" >/dev/null || { setsid nohup $R/watch_comfy.sh >/dev/null 2>&1 < /dev/null 9>&- & log "启动 Comfy看守"; }
if nvidia-smi -L 2>/dev/null | grep -q GPU; then
  if ! curl -s -m 3 -o /dev/null 127.0.0.1:8192/system_stats; then
    cd $R/ComfyUI && setsid nohup python main.py --listen 127.0.0.1 --port 8192 \
      --input-directory $R/inputs --output-directory $R/outputs > $R/comfy.log 2>&1 < /dev/null 9>&- &
    log "启动 ComfyUI"
  fi
else
  log "无显卡模式，跳过 ComfyUI"
fi

# FaceFusion / DreamID / DeepFaceLab worker always runs, including no-GPU boot.
setsid nohup /root/autodl-tmp/watch_faceswap.sh >>/root/autodl-tmp/watch_faceswap.log 2>&1 </dev/null 9>&- &

setsid nohup /root/autodl-tmp/watch_char_anim.sh >>/root/autodl-tmp/watch_char_anim.log 2>&1 </dev/null 9>&- &
setsid nohup /root/autodl-tmp/watch_motion_preview.sh >>/root/autodl-tmp/watch_motion_preview.log 2>&1 </dev/null 9>&- &
pgrep -f "autodl-tmp/watch_vlib_auto.sh" >/dev/null || { setsid nohup $R/watch_vlib_auto.sh >>$R/watch_vlib_auto.log 2>&1 < /dev/null 9>&- & log "启动 vlib自动进库"; }
log "autostart 完成"
