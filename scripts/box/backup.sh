#!/bin/sh
# Daily ledger + storage backup for the elitedesk stacks, 7-day rotation.
# Installed on elitedesk as ~/backups/free-photo-edit/backup.sh.
set -eu
: "${HOME:?HOME must be set}"
BASE="$HOME/backups/free-photo-edit"
DAY=$(date +%F)
OUT="$BASE/$DAY"
mkdir -p "$OUT"

for project in free-photo-edit-dev free-photo-edit; do
    data="$HOME/.homeploy/projects/$project/data"
    [ -d "$data" ] || continue
    if [ -f "$data/credits.db" ]; then
        # .backup is safe while the container is writing.
        sqlite3 "$data/credits.db" ".backup '$OUT/$project-credits.db'"
    fi
    if [ -d "$data/storage" ]; then
        rsync -a --delete "$data/storage/" "$OUT/$project-storage/"
    fi
done

if [ -n "${BASE:-}" ] && [ -d "$BASE" ]; then
    find "$BASE" -mindepth 1 -maxdepth 1 -type d -mtime +7 -exec rm -rf {} +
fi
echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') backup ok: $DAY"
