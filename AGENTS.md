# AGENTS.md

Single source of agent rules. `CLAUDE.md` is a symlink to this file — never edit it separately.
Architecture and layer rules live in `docs/ARCHITECTURE.md`.

## Before you start

1. Read `docs/ARCHITECTURE.md`. It is one page.
2. Work in a worktree + branch. Never commit to `master` or `dev` directly.
   Branch from `origin/dev`: `dev` is the integration branch, `master` is the
   production release pointer and is deliberately behind it.
3. Run `scripts/check.sh` before you claim anything works.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt   # once
scripts/check.sh                                                          # always
```

`check.sh` = ruff ratchet → mypy ratchet → structure/layers → anti-weakening → pytest → node checks.
It is the same command CI runs. Green locally is the only definition of done.

## Layout

| Path | What goes there |
|---|---|
| `api/` | FastAPI app, tests (`api/test_*.py`), ledger, engines, prompts |
| `web/` | Static UI only: HTML, CSS, JS. No secrets, no build step |
| `scripts/` | Repo tooling (check, ratchet, structure, guard, audit, deploy) |
| `docs/` | `ARCHITECTURE.md`, `AI_WORKFLOW.md`, `decisions/`, `memories.md` |
| `.ai/` | Ratchet baseline and metrics. Generated, reviewed, not hand-tuned |
| `DESIGN.md` | Design tokens and visual direction for the project |
| `REVISION` | Build revision stamped by `homeploy push`; served at `/api/config` |

## Dependency rule

Imports go strictly downward: `api/server.py` → `engines`/`cleanup` → `prompts`/`credits`.
Tests sit above everything. A lower layer must never import a higher one.
A new `api/` module is an architecture change: add it to `scripts/structure.py` and `docs/ARCHITECTURE.md`.

## Typing and linting

- `mypy --strict` and `ruff` are enforced per file against `.ai/baseline.json`.
- Existing files may only keep or reduce their finding count. **New files start at zero.**
- If you reduce findings, re-freeze with `python scripts/ratchet.py --update` — that is the only legitimate baseline edit, and it needs the `config-change` PR label.
- Annotate new functions fully. Prefer real types over `Any`.
- Reduce the ratchet when you touch a file; do not spend a task purely on debt unless asked.

## Do not

- Do not add a dependency. Ask first; `httpx`, `fastapi` and stdlib already cover this repo.
- Do not edit, delete, skip or soften tests to get green. Fix the code or say it is broken.
- Do not add `# type: ignore`, `# noqa`, or `pytest.mark.skip` to silence a check. Fix it.
- Do not touch `.ai/baseline.json`, `pyproject.toml`, `.github/workflows/`, or `CODEOWNERS` without the `config-change` label. A deliberate local equivalent: `ALLOW_PROTECTED=1 git commit ...`.
- Do not add a top-level folder or move files between layers without updating `docs/ARCHITECTURE.md` first.
- Do not put API keys, tokens or absolute backend hosts in `web/` or in git. `.env` stays local.
- Do not commit, push or deploy unless explicitly asked. Never `git add -A`; name paths.
- Do not change `deploy.yml` or deploy config as part of a feature change.
- Do not refactor broadly. Stay inside the paths your task names.

## Conventions

- Tests live next to the code they test in `api/`, named `test_<area>.py`.
- Every endpoint change updates `api/test_edit_contract.py` in the same PR.
- Errors from providers become `EngineError`; routes map them to HTTP status codes.
- Money/credits logic: check-then-debit inside one transaction, refund on failure.
- Comments explain *why*, one line. No commented-out code.
- Keep responses and diffs small enough for a human to review in one sitting.

<!-- design-kit:begin (managed) -->
## Design work

For any UI, page, layout, CSS, or HTML task, read `~/.agents/design/AGENTS.md` and follow it. It defines the design-token contract and the adopt/restyle workflow. Never invent a visual direction: the project `DESIGN.md` or `~/.agents/design/REGISTRY.md` decides it.
<!-- design-kit:end -->

## When a check fails

Read the message. `FAIL: layer: ...` means the import direction is wrong, not that the check is.
`FAIL: protected file modified` means your change needs a maintainer decision.
`FAIL: ... rose N -> M` means your diff added findings. Fix the diff; do not raise the baseline.
