#!/bin/bash
# H3 generate watchdog; worker itself gates on GPU + verified weights.
set -u
R=/root/autodl-tmp
exec 8>/tmp/watch_motion_h3.lock
flock -n 8 || exit 0
trap 'kill "${child:-0}" 2>/dev/null || true; exit' TERM INT
while true; do
  /root/miniconda3/bin/python "$R/studio/motion_h3_worker.py" 8>&- </dev/null >>"$R/motion_h3_worker.log" 2>&1 &
  child=$!
  wait "$child" || true
  echo "$(date '+%F %T') worker exited; restart in 3s" >> "$R/motion_h3_worker.log"
  sleep 3
done
