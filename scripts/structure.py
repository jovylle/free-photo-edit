#!/usr/bin/env python3
"""Architecture guard: dependency direction, repo layout, rules-file integrity.

Depends only on the stdlib (ast + pathlib) — the module graph is five files and
does not justify an extra dependency.

Rules enforced
  1. An api module may import only from a strictly lower LAYER (see ARCHITECTURE.md).
  2. Every api module has an assigned layer, and no unlisted module/file exists.
  3. Repo layout stays as documented (top-level entries and api/ file names).
  4. CLAUDE.md stays identical to AGENTS.md, and paths named in AGENTS.md /
     docs/ARCHITECTURE.md still exist.

To add a module: assign its layer below, add it to ARCHITECTURE.md, then this
passes. That edit is the architecture decision, not an obstacle.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = "free_photo_edit_api"

# Lower number = more foundational. An import must point strictly downward.
LAYERS = {
    0: ("__init__", "conftest", "prompts", "credits"),
    1: ("cleanup", "engines"),
    2: ("server",),
    3: ("probe_live",),  # harness/entrypoint: may import anything
}
LAYER = {name: layer for layer, names in LAYERS.items() for name in names}

API_FILES = {f"{name}.py" for name in LAYER} | {"test_abuse.py", "test_async_queue.py", "test_boot.py", "test_cleanup.py", "test_credits.py", "test_edit_contract.py", "test_engines.py"}

TOP_LEVEL = {
    ".ai", ".github", ".githooks", ".git", "api", "docs", "scripts", "tests", "web",
    ".dockerignore", ".env.example", ".gitignore",
    "AGENTS.md", "BUILD-BRIEF.md", "CLAUDE.md", "CODEOWNERS", "Dockerfile",
    "README.md", "deploy.yml", "pyproject.toml", "requirements.txt",
    "requirements-dev.txt", "sync-live.sh",
}

RULES_FILES = ("AGENTS.md", "docs/ARCHITECTURE.md")
PATH_RE = re.compile(r"`([A-Za-z0-9_][A-Za-z0-9_./-]*/?[A-Za-z0-9_.-]*)`")
ROOTS = ("api/", "web/", "docs/", "scripts/", "tests/", ".github/", ".ai/")


def module_name(path: Path) -> str:
    return path.stem


def imports_of(path: Path) -> set[str]:
    """Intra-package imports, normalised to bare module names."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom):
            if node.level:  # from .credits import X
                if node.module:
                    found.add(node.module.split(".")[0])
                continue
            if node.module == PKG:  # from free_photo_edit_api import server
                found.update(alias.name.split(".")[0] for alias in node.names)
            elif node.module and node.module.startswith(f"{PKG}."):
                found.add(node.module.split(".")[1])
        elif isinstance(node, ast.Import):  # import free_photo_edit_api.server
            for alias in node.names:
                if alias.name.startswith(f"{PKG}."):
                    found.add(alias.name.split(".")[1])
    return found


def check_layout(errors: list[str]) -> None:
    # Layout means what git tracks (plus new files not gitignored). Local caches
    # and scratch artifacts are ignored by .gitignore and must not be judged.
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT, capture_output=True, text=True,
    ).stdout
    entries = {path.split("/")[0] for path in listed.split("\0") if path}
    for entry in sorted(entries):
        if entry not in TOP_LEVEL:
            errors.append(f"layout: unexpected top-level entry {entry!r} (document it in AGENTS.md + scripts/structure.py)")
    for path in sorted((ROOT / "api").glob("*.py")):
        if path.name not in API_FILES:
            errors.append(f"layout: unexpected api file {path.name!r} (assign it a layer in scripts/structure.py)")


def check_layers(errors: list[str]) -> None:
    for path in sorted((ROOT / "api").glob("*.py")):
        name = module_name(path)
        if name in LAYER:
            source_layer = LAYER[name]
        elif name.startswith("test_"):
            source_layer = 3  # tests sit above the app
        else:
            continue  # already reported by check_layout
        for target in sorted(imports_of(path)):
            if target not in LAYER:
                errors.append(f"layer: api/{path.name} imports unknown module {target!r} — assign it a layer")
            elif LAYER[target] >= source_layer:
                errors.append(
                    f"layer: api/{path.name} (L{source_layer}) imports {target} (L{LAYER[target]}) — imports must go strictly downward"
                )


def check_rules_sync(errors: list[str]) -> None:
    agents, claude = ROOT / "AGENTS.md", ROOT / "CLAUDE.md"
    if not agents.exists():
        errors.append("rules: AGENTS.md missing — it is the single source of agent rules")
        return
    if not claude.exists():
        errors.append("rules: CLAUDE.md missing — it must point at AGENTS.md")
    elif claude.resolve() != agents.resolve() and claude.read_text() != agents.read_text():
        errors.append("rules: CLAUDE.md diverged from AGENTS.md (keep it a symlink)")

    for rel in RULES_FILES:
        path = ROOT / rel
        if not path.exists():
            errors.append(f"rules: {rel} missing")
            continue
        for token in PATH_RE.findall(path.read_text()):
            if not token.startswith(ROOTS) or any(c in token for c in "{*}<>|$ "):
                continue  # routes, branches, commands — not paths
            if not (ROOT / token.rstrip("/")).exists():
                errors.append(f"rules: {rel} names {token!r}, which does not exist")


def main() -> int:
    errors: list[str] = []
    check_layout(errors)
    check_layers(errors)
    check_rules_sync(errors)
    for error in errors:
        print(f"FAIL: {error}")
    print(f"{'FAIL' if errors else 'ok'}: structure & layers ({len(errors)} problem(s))")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
