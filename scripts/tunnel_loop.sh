#!/bin/bash
R=/root/autodl-tmp
URL=https://bbstudio6006b.serveousercontent.com
while true; do
  echo "$URL" > $R/public_url.txt
  echo "$(date '+%F %T') connect $URL" >> $R/tunnel.log
  ssh -i /root/.ssh/serveo_ed25519 -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes       -R bbstudio6006b:80:127.0.0.1:6006 serveo.net >> $R/tunnel.log 2>&1 < /dev/null
  echo "$(date '+%F %T') down, retry" >> $R/tunnel.log
  sleep 3
done
