#!/bin/bash
# 一键启动：ComfyUI(127.0.0.1:8192) + 素材包工作台网页(0.0.0.0:6006)
export PATH=/root/miniconda3/bin:$PATH HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
R=/root/autodl-tmp
export NO_TOKEN=0
mkdir -p $R/inputs $R/outputs
[ -s $R/studio_token.txt ] || python -c "import secrets;print(secrets.token_urlsafe(18))" > $R/studio_token.txt
pkill -f "python.*[m]ain.py --listen" ; pkill -f "python.*[s]erver.py" ; sleep 2
cd $R/ComfyUI
nohup python $R/ComfyUI/main.py --listen 127.0.0.1 --port 8192 \
  --input-directory $R/inputs --output-directory $R/outputs > $R/comfy.log 2>&1 &
for i in $(seq 1 90); do curl -s -o /dev/null 127.0.0.1:8192/system_stats && break; sleep 2; done
cd $R/studio
nohup python $R/studio/server.py > $R/studio.log 2>&1 &
sleep 3; curl -s -o /dev/null -w "studio http %{http_code}\n" 127.0.0.1:6006/api/health
echo "令牌: $(cat $R/studio_token.txt)"
