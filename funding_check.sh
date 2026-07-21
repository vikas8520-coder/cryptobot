#!/bin/bash
cd ~/cryptobot
source ~/cryptobot/telegram.conf
OUT=$(.venv/bin/python3 funding_monitor.py 2>&1)
AVG=$(echo "$OUT" | grep -E "AVG" | head -1 | sed 's/  */ /g')
echo "$(date '+%Y-%m-%d %H:%M') |$AVG" >> ~/cryptobot/funding_monitor.log
if echo "$OUT" | grep -q "ALERT"; then
  curl -s "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TG_CHAT}" \
    --data-urlencode "text=🟢 FUNDING CARRY ALERT — carry just turned attractive!${AVG}. Market-neutral yield worth considering (if you have capital). Run ~/cryptobot/funding_monitor.py for details." >/dev/null
fi
