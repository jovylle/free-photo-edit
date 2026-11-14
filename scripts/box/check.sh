#!/bin/sh
# Uptime probe for the dev API. Alerts Telegram on a state change only.
# Installed on elitedesk as ~/monitoring/check.sh (see docs/operations.md).
set -u
URL="https://freephoto.uft1.com/api/config"
DIR="$HOME/monitoring"
STATE="$DIR/state"
ENVF="$DIR/telegram.env"
LOG="$DIR/check.log"
mkdir -p "$DIR"

now() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }

alert() {
    echo "$(now) ALERT $1" >> "$LOG"
    [ -f "$ENVF" ] || return 0
    TELEGRAM_TOKEN=$(sed -n 's/^TELEGRAM_TOKEN=//p' "$ENVF" | head -n1)
    TELEGRAM_CHAT_ID=$(sed -n 's/^TELEGRAM_CHAT_ID=//p' "$ENVF" | head -n1)
    [ -n "${TELEGRAM_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ] || return 0
    curl -fsS --max-time 10 -X POST \
        "https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage" \
        -d "chat_id=${TELEGRAM_CHAT_ID}" -d "text=$1" >/dev/null 2>&1 || true
}

if curl -fsS --max-time 10 "$URL" >/dev/null 2>&1; then
    state=up
else
    state=down
fi

prev=$(cat "$STATE" 2>/dev/null || echo unknown)
echo "$state" > "$STATE"
echo "$(now) $state" >> "$LOG"

# First run records the baseline; it must not page anyone.
[ "$prev" = unknown ] && exit 0

if [ "$state" != "$prev" ]; then
    if [ "$state" = down ]; then
        alert "free-photo-edit dev is DOWN ($URL)"
    else
        alert "free-photo-edit dev is UP again ($URL)"
    fi
fi
