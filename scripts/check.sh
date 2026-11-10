#!/usr/bin/env bash
# One command. CI runs exactly this; run it before every PR.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

PY=${PY:-.venv/bin/python}
[ -x "$PY" ] || PY=$(command -v python3)

# Isolated fixtures only: never the real ledger, storage path or provider key.
export FREE_PHOTO_EDIT_DB=${FREE_PHOTO_EDIT_DB:-$(mktemp -d)/credits.db}
export FREE_PHOTO_EDIT_STORAGE=${FREE_PHOTO_EDIT_STORAGE:-$(mktemp -d)/storage}
export TURNSTILE_SECRET=${TURNSTILE_SECRET:-} TURNSTILE_SITEKEY=${TURNSTILE_SITEKEY:-} OPENAI_API_KEY=${OPENAI_API_KEY:-}

fail=0
step() {
  printf '\n== %s\n' "$*"
  "$@" || { echo "FAILED: $*"; fail=1; }
}

step "$PY" scripts/ratchet.py      # mypy --strict + ruff, per-file ratchet vs .ai/baseline.json
step "$PY" scripts/structure.py    # dependency layers, repo layout, AGENTS.md integrity
step "$PY" scripts/guard.py        # new suppressions, lost tests, protected-file edits
step "$PY" -m pytest api -q
step node --check web/app.js
step node tests/expired-card.cjs

if [ "$fail" -ne 0 ]; then
  printf '\ncheck FAILED\n'
  exit 1
fi
printf '\ncheck passed\n'
