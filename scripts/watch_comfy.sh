#!/bin/bash
# ComfyUI drop watchdog. Only touches 8192/ComfyUI; never user assets.
export PATH=/root/miniconda3/bin:$PATH
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
R=/root/autodl-tmp
L=$R/watch_comfy.log
URL=http://127.0.0.1:8192/system_stats
INTERVAL=60
LOCK=/tmp/watch_comfy.lock

exec 8>"$LOCK"
flock -n 8 || exit 0

log() { echo "$(date '+%F %T') $*" >> "$L"; }

start_comfy() {
  mkdir -p "$R/inputs" "$R/outputs"
  cd "$R/ComfyUI" || return 1
  setsid nohup python "$R/ComfyUI/main.py" --listen 127.0.0.1 --port 8192 \
    --input-directory "$R/inputs" --output-directory "$R/outputs" \
    > "$R/comfy.log" 2>&1 < /dev/null &
  echo $!
}

has_gpu() { nvidia-smi -L 2>/dev/null | grep -q GPU; }

alive() { curl -sS -m 5 -o /dev/null "$URL"; }

log "watch_comfy started pid=$$"

while true; do
  if ! has_gpu; then
    pkill -f "python.*[m]ain.py --listen" 2>/dev/null && log "no GPU - stopped ComfyUI"
    sleep "$INTERVAL"; continue
  fi
  if alive; then
    :
  else
    if pgrep -f "python.*[m]ain.py --listen" >/dev/null; then
      log "FAIL system_stats but process alive - stuck, restarting ComfyUI"
      pkill -f "python.*[m]ain.py --listen" || true
      sleep 2
      pkill -9 -f "python.*[m]ain.py --listen" 2>/dev/null || true
      sleep 1
    else
      log "FAIL system_stats and no process - starting ComfyUI"
    fi
    pid=$(start_comfy)
    log "started ComfyUI pid=$pid"
    ok=0
    for i in $(seq 1 90); do
      if alive; then ok=1; log "ComfyUI healthy after $i checks"; break; fi
      if ! pgrep -f "python.*[m]ain.py --listen" >/dev/null; then
        log "ComfyUI died during startup"
        break
      fi
      sleep 2
    done
    [ "$ok" = 1 ] || log "WARN ComfyUI still not healthy after wait"
  fi
  sleep "$INTERVAL"
done