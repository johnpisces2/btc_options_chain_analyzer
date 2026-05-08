#!/bin/bash
# BTC Options Headless — Cron Wrapper
# Run: every Friday at 18:00 UTC+8

set -u

VENV="/Users/tyc/Desktop/workspace/btc_options_chain_analyzer/.venv/bin/python"
SCRIPT="/Users/tyc/Desktop/workspace/btc_options_chain_analyzer/run_headless.py"
LOG_DIR="/Users/tyc/logs"
OUTPUT_DIR="/Users/tyc/Desktop/workspace/btc_options_chain_analyzer/results"
TG_BOT_TOKEN="8323301791:AAE1Sg23dJsxcyD1PFr364W5C8P1liHb2qY"
TG_CHAT_ID="406604246"

mkdir -p "$LOG_DIR" "$OUTPUT_DIR"

TODAY=$(date +%Y-%m-%d)
LOGFILE="$LOG_DIR/btc_options_${TODAY}.log"
OUTFILE="$OUTPUT_DIR/btc_options_${TODAY}.json"

send_telegram() {
    local text="$1"
    local parse_mode="${2:-HTML}"
    /usr/bin/curl -sS -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${TG_CHAT_ID}" \
        --data-urlencode "text=${text}" \
        --data-urlencode "parse_mode=${parse_mode}" \
        --data-urlencode "disable_web_page_preview=true" \
        >/dev/null
}

fail_and_notify() {
    local reason="$1"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: ${reason}" | tee -a "$LOGFILE"
    send_telegram "⚠️ <b>BTC Options Job Failed</b>
<blockquote>時間: $(date '+%Y-%m-%d %H:%M:%S')
原因: ${reason}</blockquote>
<code>${LOGFILE}</code>"
    exit 1
}

RUN_OUTPUT=$("$VENV" "$SCRIPT" \
    --mode deribit \
    --expiry nextfriday \
    --wings 5 \
    --delta 2 \
    --output "$OUTFILE" 2>&1)
RUN_EXIT=$?
printf '%s\n' "$RUN_OUTPUT" | tee -a "$LOGFILE"

if [ $RUN_EXIT -ne 0 ]; then
    fail_and_notify "run_headless.py exited with code ${RUN_EXIT}"
fi

if [ ! -f "$OUTFILE" ]; then
    fail_and_notify "output file not created"
fi

TODAY_FMT=$(date +%Y-%m-%d)
WEEK_END=$(TODAY_FMT="$TODAY_FMT" python3 - <<'PY'
from datetime import date, timedelta
import os
print((date.fromisoformat(os.environ["TODAY_FMT"]) + timedelta(days=6)).isoformat())
PY
) || fail_and_notify "failed to build week range"

SUMMARY=$(python3 -c "
import json
d = json.load(open('$OUTFILE'))
bt = d['backtest_stats']
print(f'BTC Options 週報')
print(f'Backtest Stats (±2σ)')
print(f'Samples: {bt[\"sample_count\"]} (weekly ratio changes, {bt[\"settlement_count\"]} settlements)')
print(f'Mean: {bt[\"mean\"]:+.3%}')
print(f'Std: {bt[\"std\"]:.3%}')
print(f'±2σ Band: {bt[\"lower_2sigma\"]:,.2f} ~ {bt[\"upper_2sigma\"]:,.2f} (base {bt[\"base_price\"]:,.2f})')
print(f'本週區間: $TODAY_FMT ~ $WEEK_END')
") || fail_and_notify "failed to build summary"

TG_MESSAGE=$(python3 - <<PY
import json
from html import escape
from datetime import datetime
with open('$OUTFILE') as f:
    d = json.load(f)
bt = d['backtest_stats']
now = '$TODAY_FMT'
week_end = '$WEEK_END'
outfile = '$OUTFILE'
message = f"""📊 <b>BTC Options Signal</b>

<b>Backtest Stats (±2σ)</b>
Samples <b>{bt['sample_count']}</b> (weekly ratio changes, {bt['settlement_count']} settlements)
Mean <b>{bt['mean']:+.3%}</b>
Std <b>{bt['std']:.3%}</b>
±2σ Band <b>{bt['lower_2sigma']:,.2f} ~ {bt['upper_2sigma']:,.2f}</b>
Base <b>{bt['base_price']:,.2f}</b>
週區間 <b>{now} ~ {week_end}</b>

🕒 <code>{escape(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))}</code>"""
print(message)
PY
) || fail_and_notify "failed to build telegram message"

echo "" >> "$LOGFILE"
echo "=== BTC Options — $(date '+%Y-%m-%d %H:%M') ===" >> "$LOGFILE"
echo "$SUMMARY" >> "$LOGFILE"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Done. Output: $OUTFILE" >> "$LOGFILE"

send_telegram "$TG_MESSAGE"
