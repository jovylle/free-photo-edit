# free-photo-edit — session memories

## Backup rehearsal + dev UI polish (2026-11-08)
- Live backup taken read-only via `sqlite3 .backup` to
  `~/fore/services/free-photo-edit/backups/2026-11-08-pre-migration`
  (credits.db + storage). integrity_check ok: 18 users, 15 done, 1 failed, 39
  credits, 32 files. Live service untouched.
- Restore rehearsal passed: merged code ran on a COPY of that backup; history
  and results loaded, output PNG bytes matched the stored file, and all terminal
  rows got `terminal_at`. Note opening the live DB with new code WRITES the
  schema migration, so first prod start must be intentional.
- RELEASE GOTCHA: `homeploy push` fails with "port 8090 is already in use" on
  redeploy. Run `homeploy down free-photo-edit-dev` on the box first.
- Dev UI fixes: header said "3 free/day" but credits are 3 total (not daily);
  dropzone got an icon/title/hint empty state; the "Ad slot (placeholder)" box
  is hidden until real inventory.
- Added no-cache on HTML (`_revalidate_html` middleware): browsers
  heuristic-cached index.html, hiding deploys. Data volume proven to survive
  redeploy (prior dev jobs still listed).
- 87 API tests pass; expired-card DOM test passes.

## Dev stack live on elitedesk (2026-11-08)
- `dev` merged PR #1, then deployed via homeploy: container
  `homeploy-free-photo-edit-dev-...` on host port 8090, tunnel hostname
  `https://free-photo-edit-dev.uft1.com`. Data at
  `~/.homeploy/projects/free-photo-edit-dev/data` (volume, survives redeploy).
- Dev runs `FREE_PHOTO_EDIT_ENGINE=fake` (echoes a PNG). Deliberately no
  OpenAI key and no Turnstile secret anywhere in the image, so a public
  hostname cannot spend money. Production must override the engine env.
- E2E verified on dev: POST /api/edit -> 202 -> done -> 70B PNG download.
- CI still cannot run (Actions failed in 0s, zero jobs; private-repo billing
  suspicion). Local gate = 86 API tests + expired-card; use before merges.
- Production `freephoto.uft1.com` untouched, still the Mac launchd service.

## Combined branch (2026-11-08)
- Three-way merge done in worktree `.t3/worktrees/free-photo-edit/t3-e85acc37`
  (branch `manage-deployment-plan`): lab TTL/cleanup + server hardening vs live
  multi-photo (1–5 uploads, `input/{index}`, list-typed `image`). Live staged
  app and lab checkout untouched; no production write.
- Cleanup `ARTIFACTS` widened to `input_0..4.{jpg,jpeg,png,webp}` + `output.png`
  so multi-photo inputs are swept too.
- Fixed real bug: `CreditsStore.ip_job_count_since` read the shared SQLite
  connection without `self._lock` → InterfaceError under concurrent submits
  (exposed by `test_concurrent_double_submit_ok`).
- Contract change: duplicate `image` form fields are now legal (multi-photo);
  only non-`image` file parts are rejected. Obsolete duplicate-field test
  replaced with `test_multiple_photos_accepted`.
- Verified isolated: 85 API tests pass, expired-card DOM test passes,
  `py_compile` + `node --check` clean. Temp DB/storage, no provider calls.

## Deployment planning (2026-11-08)
- Proposed phased plan: `docs/deployment-plan.md`. Separate local worktrees,
  Elitedesk dev and production, Cloudflare static frontends, PR/CI gates, and
  isolated end-to-end/regression tests. Audit dirty checkouts before changes.
- Planning only: no branch, deployment, migration, production deletion, or paid
  provider call authorized or performed by writing the plan.

## Automatic artifact retention (TTL follow-up)
- `api/credits.py` persists first `terminal_at` on complete/refund/fail. Legacy
  terminal rows get migration-time timestamps once, never inferred from mtime.
- `api/cleanup.py`: seven days after terminal time, delete only regular known
  artifacts via no-follow directory descriptors; preserve all ledger/accounting,
  running/unknown jobs, orphan dirs, symlinks and unknown content. Failures retry.
- `api/server.py` lifespan sweeps at startup/hourly, stops/joins on shutdown.
  Time-expired poll/history stay 200 with original state + artifacts_expired,
  no URLs; file/input routes return 410 even before physical deletion. UI shows
  expired history without compare/download. Stranded running recovery is separate.
- Verified: 85 API tests pass (one existing Starlette/httpx warning), Node
  expired-card done/failed checks pass, JS syntax and git diff whitespace clean.
  Tests used temporary SQLite/storage; no production cleanup, deployment, live
  AI, tunnel changes or git add/commit/push. Existing dirty changes preserved.
- Smart consultation failed with provider 403 (model access disabled); proceeded
  under headless task contract. Deployment/deletion still needs explicit approval.


## How the user works (2026-10-18)
- User SSHes into the Mac Mini (`minipc`) from another machine. They do NOT
  browse on the Mini itself.
- Anything served for their eyes (mockup servers, preview pages, the app
  itself) MUST bind `0.0.0.0` / a LAN-reachable host — never `localhost`-only.
- Reachable IPs: `192.168.1.47` (Wi-Fi), `192.168.1.2` (Ethernet),
  Tailscale `100.91.162.44`. User prefers Tailscale URLs — always give the
  Tailscale URL alongside (or instead of) the LAN one. Server binds `0.0.0.0`
  so one process covers both.
- Brainstorm visual-companion server: always start with
  `--host 0.0.0.0 --url-host 192.168.1.47` (no `--open`; headless over SSH).
  Restarting with the same `--project-dir` may create a NEW session dir —
  re-copy any screens into the new `content/` dir.

## FPE-4 frontend (2026-11-02)
- Adapted the existing UI, not a rewrite: accessible native-range before/after
  comparisons, TL/EN/Bisaya custom placeholder, native preset buttons, empty
  restore support, exact-one-file selection, POST-only duplicate-submit guard,
  429 Retry-After guidance, 404 poll stop, and small-screen header wrapping.
- All fetches stay on same-origin `/api/*`; result/download URLs are validated
  against `/api/result/{id}/file` or `/input/0` before use. No model keys added.
  Existing optional Turnstile script remains external (public sitekey, not a secret).
- `tests/frontend-smoke.js` is a Playwright MCP function, not a Node CLI test.
  Serve `web/` on 127.0.0.1:8901, then run with browser_run_code_unsafe(filename=...).
  If project paths are outside MCP allowed roots, copy the test unchanged into
  `$HOME/.hermes/cache/scratch/` and invoke that copy. Uses a fresh context and
  mocked APIs/fake clock: no real model calls, credits, or donation transactions.
- Verified 375px layout, 202 multipart, duplicate guard, exact 10s polling,
  running/completed reload resume, comparison, download, 402/403/429/502/503,
  failed/refund display, 404 poll termination, and API-only fetch guard.
  Static HTTP 200; backend suite 73 passed (existing deprecation warning).
- Reused existing GCash QR, Ko-fi destination, ad placeholder, theme and form
  persistence. No backend edits, deployment, git add/commit/push. Real Turnstile,
  real AI and FB in-app browser remain downstream/live verification.

## FPE-3 route contract (2026-11-02)
- Slots are 3 per resolved IP (shared across browser user tokens) and 6 global,
  in-process only. Existing abuse gates and atomic rolling-24h cap are unchanged.
- Reject missing, empty, duplicate or extra uploads before debit. One jpeg/png/webp
  upload up to 5 MiB is accepted. MIME is declared, not image-decoder validation.
- Post-debit mkdir/write/thread-start failure refunds and marks failed, releases
  both slots, and returns 502. Accepted asynchronous failures keep HTTP-200 polls
  with `status: failed` and no image URL (owner-approved semantics).
- Contract/queue tests use mocked engines; event gates prove running state and
  real 3/IP + 6-global capacity/reuse. Full suite: 73 passed; queue repeated ten
  times. Activate `.venv` before running `python3 -m pytest api -v`.
- Restart recovery remains absent; a killed process can strand running jobs.
  No live AI call or deployment was performed for this task.

## Test hermetic lesson (2026-10-18)
- Lab `.env` holds the REAL Turnstile secret and `load_dotenv` pulls it into
  the pytest process — 13 tests 403'd. Fixtures now force
  `TURNSTILE_SECRET=""` (and SITEKEY in the config test). Any new
  env-gated behavior needs the same treatment in fixtures.

## Global daily spend cap (2026-11-01)
- `MAX_DAILY_EDITS` defaults to 60 in a rolling 24-hour window. The route's
  early count is only a fast reject; the authoritative count must run inside
  the same `BEGIN IMMEDIATE` transaction as the credit debit and job insert,
  or concurrent requests/processes can exceed the cap.
- On exhaustion, return 503 before debit or engine invocation. The live service
  currently uses the code default because its `.env` and launchd plist do not
  override the limit.

## Session (2026-10-19) — device-keyed daily cap + pushed to GitHub
- Repo now at git@github.com:jovylle/free-photo-edit.git (private, lab convention:
  no .git existed before; .gitignore excludes .venv, storage, credits.db, .env).
- Daily cap changed from per-IP 12/day to per-device (IP + UA sha256[:32],
  `ua` column in jobs) 24/day — so friends on one WiFi share the IP but each
  get their own bucket. Minute limit (3/min) and global 60/day unchanged.
- UA is spoofable — deliberate generosity cap, not security; no per-IP hard
  ceiling was added because the global 60/day bounds spend.
- Tests: 59 pass (`api/test_abuse.py` has per-device bucket tests; _edit
  helper sends explicit `"test-ua"` header, _seed/_dev helpers mirror
  `_client_device`). Lesson: TestClient injects its own default UA unless
  overridden — seeded rows must match same UA or the 429 never fires.
- Live deploy validated: sync-live.sh → local/public 200; tokenless POST to
  /api/edit 403s (Turnstile active, fail closed).
- Privacy page promise note from 2026-10-18 still open: 7-day cleanup is a
  manual command, no automatic deletion yet.
