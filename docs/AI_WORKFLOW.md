# AI workflow

How work gets done here when much of it is done by cheap agents in parallel.

## Loop

1. **Worktree + branch per task.** Never work in the main checkout, never on
   `master` or `dev`. Branch names: `<type>/<short-slug>`.
2. **Task brief** (issue, plan file or chat) names goal, allowed paths, the
   interface being touched, acceptance command and the don'ts. A brief without
   allowed paths is not ready to start.
3. **Run `scripts/check.sh` first**, before changing anything. It must be green
   on the branch point; if it is not, that is its own fix.
4. **Implement in small slices**, running `scripts/check.sh` between slices.
5. **PR only.** One reviewer looks at the diff, not the transcript. Target
   **`dev` explicitly** — GitHub's default branch is `master` (the production
   release pointer, deliberately behind `dev`), so the web UI offers the wrong
   base by default.
6. **Merge only when `check` is green** and no protected file moved without the
   `config-change` label. GitHub does not enforce this on the free plan — see
   "Branch protection, honestly" below.

## Task template

```text
Goal:            <one sentence, observable outcome>
Allowed paths:   <exact files/dirs; everything else is out of scope>
Interface:       <endpoints, functions, layout contract that must not change>
Acceptance:      scripts/check.sh green + <the specific behavior to verify>
Don'ts:          no new deps, no test edits, no baseline/config edits, no refactors outside paths
```

A child agent that has to guess the allowed paths has already failed. Fan-out
briefs must be mechanically checkable: exact output shape and a validation
command, not prose.

## Gates (what stops a bad change)

| Gate | Where | Catches |
|---|---|---|
| ratchet | `scripts/ratchet.py` | new type/lint findings, including in new files |
| structure | `scripts/structure.py` | wrong-way imports, new top-level dirs, undocumented modules, drifted rules files |
| guard | `scripts/guard.py` | new suppressions, deleted/skipped tests, edits to protected config |
| tests | `pytest api` | behavior |
| weekly audit | `scripts/audit.sh` | slow drift: metrics vs `.ai/baseline.json` |

Protected files (`pyproject.toml`, `.github/workflows/`, `CODEOWNERS`,
`.ai/baseline.json`, the guard scripts) need the `config-change` PR label. The
label is a human saying "this change to the checks is intended".

## Branch protection, honestly

This repo is private on GitHub Free, where branch protection **and** rulesets are
unavailable — both endpoints return `403 Upgrade to GitHub Pro`. So what actually
protects `master` and `dev` is not GitHub:

| Gate | Where | Strength |
|---|---|---|
| `.githooks/pre-push` | local, per clone (`git config core.hooksPath .githooks`) | blocks direct pushes and branch deletes; `--no-verify` bypasses |
| CI on every PR and push | `.github/workflows/ci.yml` | reports a red `check`; cannot block a merge |
| CODEOWNERS | `CODEOWNERS` | auto-requests review on PRs; cannot require approval |
| `config-change` label | `scripts/guard.py` | PR fails `check` if protected files change without the label |

Consequences: a human can still merge red CI or force-push, and a fresh clone
that never ran the `hooksPath` command has no pre-push guard. The guardrails are
only as strong as the clone they run in.

If the repo goes public or the account moves to Pro, enable: PR required, required
status check `check`, required CODEOWNERS review, no force-push. Nothing in this
repo needs to change to support that.

## Monthly, 15 minutes

- `scripts/audit.sh` — is every metric flat or better?
- `git log --oneline` — any change that touched protected files? Was it labeled?
- Ratchet — did any file get worse and get waved through?
- `docs/ARCHITECTURE.md` — still true? Layers still match the code?
- Skip/todo comments and `noqa` — any new ones with no reason?

## Quarterly, plant a violation

Prove the gates are alive. On a throwaway branch, add one of: a `# type: ignore`,
an upward import, a new top-level folder, a deleted test, a loosened
`pyproject.toml`. Confirm `scripts/check.sh` fails. Revert. If any of them
passes, the gate is dead and that is the task.

## Debt policy

`.ai/baseline.json` freezes 234 mypy and 22 ruff findings from before the
guardrails existed. The number may only go down. Reduce it opportunistically when
you touch a file, re-freeze, and say so in the PR. Do not spend a task purely on
debt unless that is the task.
