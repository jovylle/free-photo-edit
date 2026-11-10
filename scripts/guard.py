#!/usr/bin/env python3
"""Anti-weakening guard: stop agents from buying green by silencing checks.

Fails when, compared to .ai/baseline.json and to the branch point:
  * new suppression comments appear (`# type: ignore`, `# noqa`, skip/xfail marks)
  * the collected test count drops (a deleted or skipped test)
  * a protected file that already existed on the base branch is modified
    (type/lint/CI/boundary config, baseline, CODEOWNERS) without the
    `config-change` label

  python scripts/guard.py                 # check (working tree vs HEAD)
  python scripts/guard.py --update        # re-freeze suppressions + test count
  BASE_REF=origin/master python scripts/guard.py   # check a branch (used by CI)

New protected files are allowed: adding a guardrail is not weakening one.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / ".ai" / "baseline.json"

SUPPRESSIONS = {
    "type_ignore": re.compile(r"#\s*type:\s*ignore"),
    "noqa": re.compile(r"#\s*noqa"),
    "skipped_tests": re.compile(r"pytest\.mark\.(skip|xfail)|\.skip\(|\.todo\("),
}
PROTECTED = (
    "pyproject.toml",
    "CODEOWNERS",
    ".ai/baseline.json",
    ".github/workflows/",
    "scripts/ratchet.py",
    "scripts/structure.py",
    "scripts/guard.py",
    "scripts/check.sh",
)


def sh(*args: str) -> str:
    return subprocess.run(args, cwd=ROOT, capture_output=True, text=True).stdout


def count_suppressions() -> dict[str, int]:
    counts = dict.fromkeys(SUPPRESSIONS, 0)
    paths = [*ROOT.glob("api/**/*.py"), *ROOT.glob("web/**/*.js"), *ROOT.glob("scripts/*.py")]
    for path in (p for p in paths if p.name != Path(__file__).name):
        text = path.read_text(errors="ignore")
        for key, pattern in SUPPRESSIONS.items():
            counts[key] += len(pattern.findall(text))
    return counts


def test_count() -> int:
    out = sh(sys.executable, "-m", "pytest", "api", "--collect-only", "-q")
    match = re.search(r"(\d+) tests? collected", out)
    if not match:
        print("FAIL: could not collect tests (pytest did not run)")
        return -1
    return int(match.group(1))


def protected_changes() -> list[str]:
    base = os.environ.get("BASE_REF", "").strip()
    # Default local mode: everything changed in the working tree/index vs HEAD.
    diff = sh("git", "diff", "--name-only", f"{base}...HEAD" if base else "HEAD")
    changed = [line for line in diff.splitlines() if line]
    if not changed:
        return []
    existed = set(sh("git", "ls-tree", "-r", "--name-only", base).splitlines()) if base else None
    hits = []
    for path in changed:
        if not any(path == p or path.startswith(p) for p in PROTECTED):
            continue
        if existed is not None and path not in existed:
            continue  # newly added guardrail
        hits.append(path)
    return hits


def main() -> int:
    suppressions, tests = count_suppressions(), test_count()

    if "--update" in sys.argv:
        BASELINE.parent.mkdir(exist_ok=True)
        data = json.loads(BASELINE.read_text()) if BASELINE.exists() else {}
        data["tests"], data["suppressions"] = tests, suppressions
        BASELINE.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        print(f"baseline updated: tests={tests} suppressions={suppressions}")
        return 0

    data = json.loads(BASELINE.read_text()) if BASELINE.exists() else {}
    failed = False

    base_suppressions = data.get("suppressions", {})
    for key, now in suppressions.items():
        was = base_suppressions.get(key, 0)
        if now > was:
            print(f"FAIL: {key} rose {was} -> {now} — fix the finding instead of silencing it")
            failed = True

    base_tests = data.get("tests", 0)
    if tests < base_tests:
        print(f"FAIL: test count dropped {base_tests} -> {tests} — restore the test or explain in the PR")
        failed = True

    if tests >= 0 and not failed:
        print(f"ok: suppressions {suppressions}, tests {tests}/{base_tests}")

    allow = "--allow-protected" in sys.argv or os.environ.get("ALLOW_PROTECTED") == "1"
    hits = protected_changes()
    if hits and not allow:
        for hit in hits:
            print(f"FAIL: protected file modified: {hit} — needs the `config-change` PR label")
        failed = True
    elif hits:
        print(f"note: protected files changed with approval: {', '.join(hits)}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
