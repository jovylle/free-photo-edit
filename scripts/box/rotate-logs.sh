#!/bin/sh
# Copy-truncate rotation for the launchd-managed service logs.
# launchd keeps the write fd open, so renaming a log would leak it; copying
# then truncating in place bounds the live file without restarting anything.
# Only the two log files are touched, never credits.db or api/storage.
set -eu
: "${HOME:?HOME must be set}"
DIR=${1:-"$HOME/Library/Logs/fore"}
MAX_BYTES=${MAX_BYTES:-1048576}
KEEP=${KEEP:-7}
STAMP=$(date +%Y%m%d-%H%M%S)

for name in free-photo-edit.log free-photo-edit.err; do
    file="$DIR/$name"
    [ -f "$file" ] || continue
    size=$(wc -c < "$file" | tr -d ' ')
    [ "$size" -ge "$MAX_BYTES" ] || continue
    cp "$file" "$file.$STAMP"
    : > "$file"
done

for name in free-photo-edit.log free-photo-edit.err; do
    # Newest first; drop everything past the retained-copy cap.
    ls -1t "$DIR/$name".* 2>/dev/null \
        | tail -n +"$((KEEP + 1))" \
        | while IFS= read -r old; do rm -f "$old"; done
done
