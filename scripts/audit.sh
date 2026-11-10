#!/usr/bin/env bash
# Read-only audit. Writes .ai/metrics.json and exits 1 when a metric is worse
# than .ai/baseline.json. Never edits code. The weekly CI job runs this.
set -u
cd "$(dirname "$0")/.." || exit 1
mkdir -p .ai

PY=${PY:-.venv/bin/python}
[ -x "$PY" ] || PY=$(command -v python3)

# Same isolated fixtures as scripts/check.sh: never the real ledger or storage.
export FREE_PHOTO_EDIT_DB=${FREE_PHOTO_EDIT_DB:-$(mktemp -d)/credits.db}
export FREE_PHOTO_EDIT_STORAGE=${FREE_PHOTO_EDIT_STORAGE:-$(mktemp -d)/storage}

mypy_errors=$("$PY" -m mypy 2>/dev/null | grep -c ": error:")
ruff_errors=$("$PY" -m ruff check --output-format=concise 2>/dev/null | grep -cE "^[^:]+\.py:[0-9]+:[0-9]+: ")
tests=$("$PY" -m pytest api --collect-only -q 2>/dev/null | grep -oE "[0-9]+ tests? collected" | grep -oE "^[0-9]+")
# Same corpus and exclusions as scripts/guard.py (which owns the baseline numbers).
py_ignore=$(grep -rEc "#\s*type:\s*ignore" api web scripts --exclude=guard.py 2>/dev/null | awk -F: '{s+=$2} END {print s+0}')
noqa=$(grep -rEc "#\s*noqa" api web scripts --exclude=guard.py 2>/dev/null | awk -F: '{s+=$2} END {print s+0}')
skipped=$(grep -rEc "pytest\.mark\.(skip|xfail)|\.skip\(|\.todo\(" api web scripts --exclude=guard.py 2>/dev/null | awk -F: '{s+=$2} END {print s+0}')
rules_sync=ok
{ [ -f AGENTS.md ] && [ -f CLAUDE.md ] && cmp -s AGENTS.md CLAUDE.md; } || rules_sync=MISSING
protected_30d=$(git log --since='30 days ago' --name-only --pretty=format: 2>/dev/null \
  | grep -E "^(pyproject\.toml|CODEOWNERS|\.ai/baseline\.json|\.github/workflows/|scripts/(ratchet|structure|guard)\.py|scripts/check\.sh)" \
  | sort -u | wc -l | tr -d ' ')

cat > .ai/metrics.json <<JSON
{"mypy_errors":${mypy_errors:-0},"ruff_errors":${ruff_errors:-0},"tests":${tests:-0},"py_type_ignore":${py_ignore:-0},"noqa":${noqa:-0},"skipped_tests":${skipped:-0},"protected_files_changed_30d":${protected_30d:-0},"rules_sync":"${rules_sync}"}
JSON
cat .ai/metrics.json; echo

fail=0
[ "$rules_sync" != "ok" ] && { echo "FAIL: rules_sync=$rules_sync"; fail=1; }

if [ ! -f .ai/baseline.json ]; then
  echo "NOTE: no .ai/baseline.json — copy .ai/metrics.json to baseline.json to start the ratchet."
  exit $fail
fi

# Lower is better for findings, higher is better for test count.
"$PY" - <<'PY' || fail=1
import json

metrics = json.load(open(".ai/metrics.json"))
base = json.load(open(".ai/baseline.json"))
suppressions = base.get("suppressions", {})
checks = [
    ("mypy_errors", base.get("mypy", {}).get("total", 0), True),
    ("ruff_errors", base.get("ruff", {}).get("total", 0), True),
    ("py_type_ignore", suppressions.get("type_ignore", 0), True),
    ("noqa", suppressions.get("noqa", 0), True),
    ("skipped_tests", suppressions.get("skipped_tests", 0), True),
    ("tests", base.get("tests", 0), False),
]
bad = False
for key, was, lower_is_better in checks:
    now = metrics.get(key, 0)
    if (now > was) if lower_is_better else (now < was):
        print(f"FAIL: {key} {'rose' if lower_is_better else 'dropped'} {was} -> {now}")
        bad = True
raise SystemExit(1 if bad else 0)
PY

exit $fail
