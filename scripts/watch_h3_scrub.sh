#!/bin/bash
# Independent H3 scratch scrubber — does NOT touch motion_h3_worker / Comfy / studio.
# Safe while a GPU job is running: only deletes _h3work / motion_generate_h3 / _segs_*
# for job ids that are NOT queued|running|engine_pending in motion_generate_queue.
set -u
R=/root/autodl-tmp
exec 9>/tmp/watch_h3_scrub.lock
flock -n 9 || exit 0
GAP="${H3_SCRUB_WATCH_SEC:-300}"
while true; do
  /root/miniconda3/bin/python "$R/studio/motion_h3_worker.py" --scrub-once \
    9>&- </dev/null >>"$R/motion_h3_worker.log" 2>&1 || true
  sleep "$GAP"
done
