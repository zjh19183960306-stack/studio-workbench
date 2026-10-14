#!/bin/bash
cd /root/autodl-tmp/studio
# stop prior vlib auto-link watcher so restart is clean
pkill -f "/root/autodl-tmp/watch_vlib_auto.sh" 2>/dev/null || true
pkill -f "studio/scripts/vlib_auto_link.py" 2>/dev/null || true
sleep 1

# FaceFusion / DreamID / DeepFaceLab worker always runs, including no-GPU boot.
setsid nohup /root/autodl-tmp/watch_faceswap.sh >>/root/autodl-tmp/watch_faceswap.log 2>&1 </dev/null 9>&- &

setsid nohup /root/autodl-tmp/watch_char_anim.sh >>/root/autodl-tmp/watch_char_anim.log 2>&1 </dev/null 9>&- &
setsid nohup /root/autodl-tmp/watch_motion_preview.sh >>/root/autodl-tmp/watch_motion_preview.log 2>&1 </dev/null 9>&- &

setsid nohup /root/autodl-tmp/watch_vlib_auto.sh >>/root/autodl-tmp/watch_vlib_auto.log 2>&1 </dev/null 9>&- &
export NO_TOKEN=0
PW=$(tr -d "\n" < /root/autodl-tmp/studio_token.txt)
curl -s -c /tmp/rc -H "Content-Type: application/json" -d "{\"password\":\"$PW\"}" 127.0.0.1:6006/api/login >/dev/null 2>&1 || true
S=$(curl -s -b /tmp/rc 127.0.0.1:6006/api/pack 2>/dev/null)
case "$S" in *running*) echo BUSY; exit 1;; esac
unset S
for p in $(pgrep -f "python server.py"); do kill $p; done; sleep 2
setsid nohup env NO_TOKEN=0 /root/miniconda3/bin/python server.py > ../studio.log 2>&1 < /dev/null &
sleep 5; curl -s -o /dev/null -w "cloud / %{http_code}\n" 127.0.0.1:6006/
