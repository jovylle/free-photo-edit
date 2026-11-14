# free-photo-edit — session memories

## Dev split: Pages frontend + Elitedesk API (2026-11-08)
- Dev now matches the plan: static UI on Cloudflare Pages
  `https://free-photo-edit-dev.pages.dev`, API on
  `https://free-photo-edit-dev.uft1.com` (same container, /api only in practice).
- Frontend contract: `meta[name=api-base]` in each HTML page; empty means
  same-origin (container). app.js prefixes fetches AND result image URLs with
  it. Rebuild/deploy via `scripts/deploy-dev-frontend.sh [api-base]`.
- Backend CORS: `CORS_ORIGINS` (comma-separated) enables ACAO plus an OPTIONS
  preflight for non-matching origins. Dev value lives in the box env file
  `~/.homeploy/projects/free-photo-edit-dev/.env`, not in the image.
- Fixed en route: `resultUrl` only allowed `input/0`, so multi-photo history
  images threw "Invalid result URL"; now `input/\d+`.
- Verified on Pages: credits/presets load cross-origin, edit -> done, both
  comparison images render (naturalWidth 480), no console errors.

## Dev E2E verification (2026-11-08)
- Automated: 87 API tests pass (temp DB/storage, fake provider). Contract
  checks on the live dev URL: bad MIME 400, empty prompt 400, >5 photos 400,
  unknown preset 400, oversize 400, out-of-credits 402 (verified on a local
  FREE_CREDITS=1 instance; on dev the per-IP minute limit interferes).
- Real-user browser run on dev passed: upload -> preset -> submit -> 202 ->
  done, credit debit, comparison slider, download, history resume after
  reload, IndexedDB draft restore, multi-photo (2 inputs on one job),
  no-photo guard "Add a photo first."
- Fake engine echoes the uploaded PNG (or a 1x1 PNG for non-PNG input), so it
  proves plumbing only — never AI quality.
- Fixed: missing favicon caused a 404 console error on every page (inline SVG
  now, all four pages). Two apparent spacing bugs ("✅custom", "📷Edit
  another") were emoji font rendering; the DOM strings have the space.
- Housekeeping: an earlier `git add -A` swept Playwright logs and screenshots
  into history; removed and added to .gitignore. Use explicit paths, not -A.

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

## AI durability guardrails (2026-11-11)
- Branch `chore/ai-durability-setup`. One command: `scripts/check.sh` (same as CI):
  ruff ratchet -> mypy ratchet -> structure/layers -> anti-weakening -> pytest -> node.
- Baseline frozen in `.ai/baseline.json`: 234 `mypy --strict` findings (per file),
  22 ruff findings (select E4,E7,E9,F,I), 89 tests, 6 `noqa`, 0 type-ignores/skips.
  Ratchet is per file so a clean new file cannot mask a dirty old one; new files
  start at zero. Re-freeze only via `python scripts/ratchet.py --update` +
  `python scripts/guard.py --update`, with the `config-change` PR label.
- Layers (`scripts/structure.py`, AST-based, no new dep): prompts/credits L0 ->
  engines/cleanup L1 -> server L2 -> tests/probe_live L3; imports strictly down.
  A new `api/` module fails until it is assigned a layer and documented.
- `scripts/guard.py`: new suppressions, lost tests, and edits to protected files
  (pyproject, workflows, CODEOWNERS, baseline, guard scripts) fail. Locally the
  `config-change` label equivalent is `ALLOW_PROTECTED=1`; in CI it is the label.
- Layout is judged from `git ls-files` (+ unignored new files), so caches never
  trip it. `.mypy_cache/` and `.ruff_cache/` added to `.gitignore`.
- Debt not fixed (deliberate): 234 mypy + 22 ruff findings, mostly test
  annotations. Reduce opportunistically when touching a file.
- Rebased onto `origin/dev` (`d392697`) after finding two things: the branch was
  2 commits stale, and its `ci.yml` had reintroduced the bug PR #4 fixed —
  `${{ runner.temp }}` in job-level `env` invalidates the whole workflow, so CI
  never starts. Rule: `runner.temp` only in step scope; `scripts/check.sh` makes
  its own `mktemp` dirs, so no step env is needed. Same bug was in `audit.yml`
  and is fixed; `audit.sh` now exports isolated fixtures itself.
- `master` stays the default branch by maintainer decision: it is the production
  release pointer, deliberately behind `dev`. PRs must target `dev` explicitly;
  web UI offers `master`.
- Manual step per new clone: `git config core.hooksPath .githooks`.
- CORRECTION (2026-11-11): branch protection IS enabled on `dev` and `master` — PR
  required, required status check `check`, 0 approvals, force-push and deletion
  blocked, admin bypass allowed. The earlier "protection impossible" note came
  from this endpoint returning `403 Upgrade to GitHub Pro` twice on this
  private/free repo while protection was active and enforcing; do not trust that
  403. The first merge attempt was blocked because protection still required the
  pre-rename check name `test`; fixed by PATCHing `required_status_checks` to
  `check` on both branches. The required context is the CI **job** name — rename
  the job and the protection together.

## 2026-11-14 — emoji removal, icon sprite, 1024 shell
- Presets are filled chips again (`index.html` `.preset`), each with a sprite icon; not text links.
- `web/icons.svg` is the only icon source; `.ico` class lives in `site.css`. See ADR 0002.
- Page shell width is 1024px everywhere (`header`, `main`, `.site-head`, `.site-foot`, `.page`).
- Cache-bust rule: when `site.js`/`app.js`/`site.css` changes, bump the `?v=` on the `<script>`/`<link>` in all 5 HTML files — CF edge caches them 4h and origin purge is broken (API token auth error).
- Social card: `scripts/og-card.html` renders `web/og-card.png` (1200x630, lavender, "5 free edits a day"). `og:image`/`twitter:image` point at `/og-card.png`; the old `/og.png` was deleted because social scrapers cache by URL. Re-render by opening the HTML at 1200x630 in a browser and screenshotting (fonts must be loaded, else it falls back to system-ui).
- Dark theme now real: `[data-theme="dark"]` token block in `tokens.css` (the toggle had no CSS behind it before). Anything with `background:var(--ink)` must use `color:var(--bg)` so it inverts (active preset chip, thumb remove button). Links/`.dl`/`#themeBtn` use `--link` (accent-light in dark) — plain `--accent` is too dark on the dark surface. `::placeholder` uses `--placeholder` for AA (light 7.3:1, dark measured >=4.5).
- `robots.txt` + `sitemap.xml` added (sitemap lists the 5 pages, robots disallows /api/).
- Privacy (2026-11-14): `/api/result/{id}/file` and `/input/{n}` now require `user` and 404 unless it matches the job owner; metadata `/api/result/{id}` stays public (status/preset only, no image). Client already appends `?user=` via `resultUrl()`, so nothing else changed. Tests updated + `test_result_file_is_owner_only` added.
- Showcase: `web/showcase.json` (array of `{image, preset, note}`) + files in `web/showcase/`. Rendered by `loadShowcase()` in app.js; section stays hidden when the array is empty. Only owner-chosen images go here — never a user's job.
- Dev (2026-11-14): the elitedesk homeploy container `homeploy-free-photo-edit-dev-free-photo-edit-dev-1` is STOPPED. Dev Pages now points at `https://freephoto.uft1.com` (prod API) and prod allows CORS for `*.free-photo-edit-dev.pages.dev` only. Turnstile widget domains updated to include `free-photo-edit-dev.pages.dev`. See ADR 0003. Preview hashes (`<hash>.free-photo-edit-dev.pages.dev`) are not allowed by Turnstile — test at the canonical URL.
- Retired the homeploy dev stack (2026-11-14): container stopped on elitedesk, DNS CNAMEs for free-photo-edit-dev.uft1.com and free-photo-edit.uft1.com deleted (originals in docs/retired/dns-records-dev-2026-11-14.json), deploy.yml moved to docs/retired/. Those hostnames now hit the `*.uft1.com` wildcard (`AAAA 100::`) and return a Cloudflare error page — expected. docs/operations.md rewritten for the single-backend layout.
