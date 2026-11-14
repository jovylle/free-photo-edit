#!/usr/bin/env python3
"""Per-file ratchet for mypy --strict and ruff.

Fails when a file gains findings relative to .ai/baseline.json. Per-file, not
total: a total-count ratchet lets a new file add errors while an old file loses
them. A file absent from the baseline has an implicit budget of 0, so new code
must be clean.

  python scripts/ratchet.py            # check (exit 1 if any file got worse)
  python scripts/ratchet.py --update   # re-freeze current counts (needs review)
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / ".ai" / "baseline.json"

MYPY_RE = re.compile(r"^(?P<file>[^:]+\.py):\d+: error:")
RUFF_RE = re.compile(r"^(?P<file>[^:]+\.py):\d+:\d+: [A-Z]+\d+")


def run(*args: str) -> str:
    return subprocess.run(
        [sys.executable, *args], cwd=ROOT, capture_output=True, text=True
    ).stdout


def collect() -> tuple[dict[str, dict[str, int]], dict[str, list[str]]]:
    raw = {
        "mypy": run("-m", "mypy").splitlines(),
        "ruff": run("-m", "ruff", "check", "--output-format=concise").splitlines(),
    }
    patterns = {"mypy": MYPY_RE, "ruff": RUFF_RE}
    tools: dict[str, dict[str, int]] = {}
    for name, lines in raw.items():
        counts: Counter[str] = Counter()
        for line in lines:
            match = patterns[name].match(line)
            if match:
                counts[match.group("file")] += 1
        tools[name] = dict(counts)
    return tools, raw


def main() -> int:
    current, raw = collect()
    total = {name: sum(files.values()) for name, files in current.items()}

    if "--update" in sys.argv:
        BASELINE.parent.mkdir(exist_ok=True)
        data = json.loads(BASELINE.read_text()) if BASELINE.exists() else {}
        for name, files in current.items():
            data[name] = {"total": total[name], "files": files}
        BASELINE.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        print(f"baseline updated: {total}")
        return 0

    if not BASELINE.exists():
        print("FAIL: .ai/baseline.json missing — run: python scripts/ratchet.py --update")
        return 1

    data = json.loads(BASELINE.read_text())
    failed = False
    for name, files in current.items():
        base = data.get(name, {})
        budget = base.get("files", {})
        worse: set[str] = set()
        for path in sorted(set(files) | set(budget)):
            was, now = budget.get(path, 0), files.get(path, 0)
            if now > was:
                print(f"FAIL: {name} {path}: {was} -> {now} findings")
                worse.add(path)
                failed = True
        if total[name] > base.get("total", 0):
            print(f"FAIL: {name} total {base.get('total', 0)} -> {total[name]}")
            failed = True
        else:
            print(f"ok: {name} {total[name]}/{base.get('total', 0)} findings (at or below baseline)")
        for line in raw[name]:
            if any(line.startswith(path) for path in worse):
                print(f"  {line}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
