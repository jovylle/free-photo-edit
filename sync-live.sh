#!/bin/bash
# Push repo -> live HOME copy + restart service. Run after every change.
# Never copies .env, credits.db, or storage (live secrets + history stay).
set -euo pipefail
SRC=/Volumes/DevSSD/fore/lab/free-photo-edit
DST=/Users/jovyllebermudez/fore/services/free-photo-edit/app
# --delete over uncommitted live work destroys the only copy of it (2026-11-20).
if [ -n "$(git -C "$DST" status --porcelain)" ]; then
  echo "sync-live: $DST has uncommitted changes — commit and push them first." >&2
  exit 1
fi
rsync -a --delete \
  --exclude='.venv/' --exclude='__pycache__/' --exclude='.pytest_cache/' \
  --exclude='.ruff_cache/' --exclude='.mypy_cache/' --exclude='*.pyc' \
  --exclude='.DS_Store' \
  --exclude='.env' --exclude='api/credits.db*' --exclude='api/storage/' \
  "$SRC/" "$DST/"
launchctl kickstart -k gui/"$(id -u)"/com.fore.free-photo-edit
sleep 5
curl -s -o /dev/null -w 'local:%{http_code}\n' http://127.0.0.1:8078/api/config
curl -s -o /dev/null -m 15 -w 'public:%{http_code}\n' https://freephoto.uft1.com/api/config
