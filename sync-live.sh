#!/bin/bash
# Push repo -> live HOME copy + restart service. Run after every change.
# Never copies .env, credits.db, or storage (live secrets + history stay).
set -euo pipefail
SRC=/Volumes/DevSSD/fore/lab/free-photo-edit
DST=/Users/jovyllebermudez/fore/services/free-photo-edit/app
rsync -a --delete \
  --exclude='.venv/' --exclude='__pycache__/' --exclude='.pytest_cache/' \
  --exclude='.env' --exclude='api/credits.db*' --exclude='api/storage/' \
  "$SRC/" "$DST/"
launchctl kickstart -k gui/"$(id -u)"/com.fore.free-photo-edit
sleep 5
curl -s -o /dev/null -w 'local:%{http_code}\n' http://127.0.0.1:8078/api/config
curl -s -o /dev/null -m 15 -w 'public:%{http_code}\n' https://freephoto.uft1.com/api/config
