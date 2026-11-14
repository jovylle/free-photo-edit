# Free Photo Edit

A self-hosted web app that edits photos from a typed instruction: upload one to five images, describe the change in Tagalog, English or Bisaya, and get an AI-edited image back with a before/after slider and a download button. One FastAPI process serves both the JSON API and the static UI, so there is no build step, no account system and no database server — state is a single SQLite file.

## Run it

Python 3.12 is what CI and the Dockerfile use. Node is only for the repo checks.

```bash
git clone https://github.com/jovylle/free-photo-edit.git
cd free-photo-edit
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env
# In .env: set OPENAI_API_KEY for real edits, or set
# FREE_PHOTO_EDIT_ENGINE=fake for a zero-cost local run.

.venv/bin/python -m uvicorn api.server:app --host 127.0.0.1 --port 8078
```

Open <http://127.0.0.1:8078>. The runtime module is `api.server:app`; `free_photo_edit_api` is a pytest-only alias from `api/conftest.py`, not an importable uvicorn package. Pass `--port` explicitly — `PORT` in `.env` is not read by uvicorn.

The `fake` engine echoes your first uploaded PNG (a 1×1 PNG for non-PNG input), so upload, the 202 job, polling, compare, download and retention all work with no key and no spend. Real edits need the default `openai` engine and a key. For trusted-LAN testing only, `--host 0.0.0.0` serves `http://<host-lan-ip>:8078`; never port-forward it. Leave Turnstile secrets blank only in isolated local dev.

## What it does

- 1–5 JPEG/PNG/WebP photos per job, 5 MiB each, plus a 2000-character prompt.
- Four presets — `background`, `clothes`, `restore`, `custom`. The preset supplies the prompt template and your text is quoted verbatim; `restore` works without text, `background` and `custom` require it.
- Async jobs: the upload returns a job ID immediately, the page polls every ~10 s.
- 3 free edits per browser token, the newest 30 jobs as history restored after reload, before/after compare and PNG download.
- Optional Cloudflare Turnstile gate and optional paid edit packs via Xendit (test keys first; live keys are a separate decision).
- Plain HTML/CSS/JS pages (`index`, `about`, `faq`, `privacy`, `terms`), light/dark theme, GCash QR and Ko-fi links.

## How it works

```text
browser (web/)
  │  POST /api/edit  multipart: image[] + prompt + preset + user
  ▼
api/server.py → Turnstile → rolling rate gates → 3-per-IP / 6-global slots
  │             → atomic check-and-debit + job insert in SQLite
  ▼  202 {job_id, status: "running", credits_left}
daemon thread → api/engines.py → provider → api/storage/<job>/output.png
  ▲
  └── browser polls GET /api/result/<job_id> → done | failed
```

A failed engine or setup call refunds the credit and marks the job `failed`; the poll then returns HTTP 200 with `status: "failed"` and a null `image_url`.

| Path | Role |
|---|---|
| `api/server.py` | FastAPI app, all routes, rate gates, job slots, worker thread |
| `api/prompts.py` | Preset templates, identity invariants, prompt assembly |
| `api/credits.py` | SQLite ledger: users, jobs, payments; atomic debit and refund |
| `api/engines.py` | Provider adapters behind `Engine.edit() -> png bytes` |
| `api/cleanup.py` | Seven-day artifact expiry and deletion sweep |
| `api/probe_live.py` | Manual one-call provider probe, never part of the app |
| `web/` | Static UI; talks only to `/api/*` on its configured origin |

Imports go strictly downward: `server` → `cleanup`/`engines` → `credits`/`prompts`. A new `api/` module is an architecture change; see `docs/ARCHITECTURE.md`.

## HTTP API

| Method | Path | Contract |
|---|---|---|
| `POST` | `/api/edit` | Multipart: `image` (1–5), `prompt`, `preset`, `user`, and `cf-turnstile-response` when Turnstile is on. Returns **202** `{job_id, status: "running", credits_left, note}`. |
| `GET` | `/api/result/{job_id}` | `{job_id, status, preset, created_at, image_url, credits_left, artifacts_expired}`. `status` is `running`, `done` or `failed`; `image_url` is null until done or after expiry. Unknown job: 404. |
| `GET` | `/api/result/{job_id}/file` | Output PNG. 410 after terminal-job expiry, otherwise 404 until done or if absent. |
| `GET` | `/api/result/{job_id}/input/{index}` | Original uploaded bytes (not a thumbnail), with JPEG/PNG/WebP MIME. 410 after expiry, otherwise 404. |
| `GET` | `/api/jobs?user=` | Up to 30 newest jobs for history and resume. |
| `GET` | `/api/credits?user=` | `{user, credits_left}`. Read routes accept `X-User` or `?user=`; POST uses the multipart `user`. |
| `GET` | `/api/presets` | The four presets with labels and hints. |
| `GET` | `/api/config` | `{turnstile_sitekey}` (null when unset); never exposes the secret. |
| `GET` | `/api/packs` | `{packs: [{id, label, price, credits}]}`, price in centavos; empty when payments are unconfigured. |
| `POST` | `/api/checkout` | JSON `{pack, user, email}` → `{checkout_url, session_id}`. 400 bad pack/email, 429 over 5/hour/IP, 503 when Xendit keys or return URLs are unset. |
| `POST` | `/api/xendit/webhook` | `x-callback-token` header; credits a pack once per paid session. 401 bad token, 400 low amount, 200 for duplicates and unknown sessions. |

Packs are priced server-side: Starter ₱49 / 10 edits, Plus ₱99 / 25, Pro ₱199 / 60. The browser never sends a price.

Errors: **400** validation, **402** no credits, **403** bot check, **429** rate or concurrency gate, **503** global daily cap or unconfigured payments, **502** post-debit setup failure (refunded). 429/503 carry `Retry-After`.

## Limits and quotas

| Limit | Value | Scope |
|---|---|---|
| Free edits | 3 | Per user token, initial grant — not a daily refill |
| Concurrent jobs | 3 per IP, 6 global | Per process, shared across user tokens |
| Submissions | 3 / minute | Per IP |
| Submissions | 24 / day | Per IP |
| Submissions | 12 / day | Per IP + User-Agent hash ("device") |
| Submissions | 60 / day | Global, all recorded jobs including failed ones |
| Uploads | 1–5 photos, 5 MiB each | Per job |
| Prompt | 2000 characters | Per job |
| Checkout attempts | 5 / hour | Per IP |
| History | 30 newest jobs | Per user token |

All windows are rolling; there is no midnight reset. One accepted edit debits one credit; engine or setup failure refunds it. Debit, job insert and the global check share one `BEGIN IMMEDIATE` transaction, so a zero balance returns 402 without an engine call.

A paid balance skips the global cap and the per-IP/per-device daily gates, because a purchased balance is already finite; the per-minute IP limit, Turnstile and upload validation still apply. The "user" is a self-typed string in SQLite and browser storage — a quota bucket, not identity. IP limits are a generosity gate, not security: `CF-Connecting-IP`/`X-Forwarded-For` are trusted as sent, so run behind a proxy you control.

## Configuration

Environment variables, loaded from the gitignored `.env` at the repo root. Never put keys in `web/`, in commands, or in git.

| Variable | Default | Meaning |
|---|---|---|
| `FREE_PHOTO_EDIT_ENGINE` | `openai` | `openai`, `workers-ai`, `pollinations`, `fake` |
| `OPENAI_API_KEY` | empty | Required by the default engine |
| `CF_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN` | empty | Workers AI credentials |
| `WORKERS_AI_MODEL` | `@cf/black-forest-labs/flux-2-dev` | Workers AI override |
| `FREE_CREDITS` | `3` | Initial credits per new user token |
| `FREE_PHOTO_EDIT_DB` | `./api/credits.db` | SQLite ledger path |
| `FREE_PHOTO_EDIT_STORAGE` | `./api/storage` | Artifact root |
| `IP_MINUTE_LIMIT` | `3` | Submissions per rolling minute per IP |
| `IP_DAILY_LIMIT` | `24` | Submissions per rolling 24 h per IP |
| `DEVICE_DAILY_LIMIT` | `12` | Submissions per rolling 24 h per IP+UA |
| `MAX_DAILY_EDITS` | `60` | Global submissions per rolling 24 h |
| `TURNSTILE_SITEKEY`, `TURNSTILE_SECRET` | empty | Public widget key / verifier secret; blank secret disables the check |
| `TURNSTILE_HOSTNAMES` | empty | Comma-separated hostnames accepted by siteverify |
| `XENDIT_SECRET_KEY`, `XENDIT_WEBHOOK_TOKEN` | empty | Payment sessions key / static callback token |
| `PAYMENT_SUCCESS_URL`, `PAYMENT_CANCEL_URL` | empty | HTTPS return URLs on the frontend origin |
| `CHECKOUT_PER_HOUR` | `5` | Checkout attempts per IP per hour |
| `CORS_ORIGINS` | empty | Extra origins allowed to call `/api/*` |
| `PORT` | `8078` | Documentation only; uvicorn's `--port` decides |

Relative overrides resolve from the server working directory. Keep the ledger and storage outside any directory a redeploy replaces. `DEVICE_DAILY_LIMIT` and `CORS_ORIGINS` exist in code but not in `.env.example`.

## Engines

- `openai` (default): `images/edits`, model `gpt-image-2.5-sunburst`, 1024×1024, quality `medium`, 1–5 images. `input_fidelity` is deliberately never sent; the key stays server-side. Historical probes estimated roughly 2.6¢/edit — not a current price guarantee and not a dollar cap. Routes record no provider usage or cost, so real spend is visible only in the OpenAI dashboard.
- `workers-ai`: Cloudflare Workers AI REST, 512×512, 25 steps, one image only. Its live image-input path was reported broken in October 2026 probes and has not been re-verified working. Explicit alternative, never automatic failover.
- `pollinations`: a stub that raises `501 not-configured`, surfaced as a failed, refunded job. `fake`: dev-only; echoes a PNG so end-to-end runs cost nothing — the Dockerfile and `deploy.yml` select it.

`api/probe_live.py` makes one real call with the selected engine — by default a paid OpenAI call — and bypasses routes, credit accounting and the daily cap. It is a probe, not a route test.

## Storage and retention

Artifacts live at `api/storage/<job_id>/input_<n>.<ext>` and `output.png`; the ledger is `api/credits.db` with `users`, `jobs` and `payments` tables.

Artifacts expire **seven days after a job first becomes `done` or `failed`** (`terminal_at + 604800`, inclusive); submission time and file mtime do not matter. A sweep runs at startup and hourly in one background thread and removes only the known artifact filenames of expired, ledger-backed terminal jobs, then the empty job directory. Running jobs, orphan directories, unknown files, nested directories and symlinks are left alone, using descriptor-relative no-follow access.

Expiry is enforced by time even before deletion succeeds: the poll and history stay HTTP 200 with the original status, `artifacts_expired: true`, a null `image_url` and empty `inputs`, while both artifact routes return **410 Gone**. Expired history is labelled and its compare/download controls are hidden.

The sweep deletes files, not records: job rows, balances, refunds, prompts and quota history are never pruned, so prompts and user tokens stay in SQLite indefinitely. Uploads are stored and forwarded byte-for-byte, so EXIF metadata — including GPS — is not stripped. Physical cleanup needs a running server and writable storage, so expiry is not an exact-time erasure guarantee.

## Deploying

Both shapes serve the same `web/` directory; only the origin differs.

**Same origin, one Mac.** Uvicorn on `127.0.0.1:8078` behind a Cloudflare tunnel at `freephoto.uft1.com`, kept alive by launchd. The established setup uses launchd `com.fore.free-photo-edit`, a staged copy at `~/fore/services/free-photo-edit/app` (launchd cannot read the external lab volume), a staged `.env`, `sync-live.sh` to rsync and restart, and logs at `~/Library/Logs/fore/free-photo-edit.{log,err}`. `sync-live.sh` never copies `.env`, `credits.db` or `storage/`. Treat these notes as the shape of the setup, not a fresh health check.

**Split: static frontend, separate API origin.** Publish `web/` as-is on Cloudflare Pages, set the `content` of the `api-base` meta tag in each page to the API origin (empty means same origin), and allow that origin on the API with `CORS_ORIGINS=https://your-frontend.example`. `app.js` prefixes API calls and result URLs with that base, so no host is baked into the code. Without a matching `CORS_ORIGINS` entry the response has no `Access-Control-Allow-Origin` and the browser blocks it. Payment return URLs must be HTTPS on the frontend origin.

**Container.** The `Dockerfile` serves API plus UI on port 8080 with `FREE_PHOTO_EDIT_ENGINE=fake` and data under `/app/data`; override both for anything real and mount `/app/data` as a volume so a redeploy cannot wipe the ledger. `deploy.yml` describes the private dev stack (`free-photo-edit-dev`, `8090:8080`, health check `/api/config`); production rollout goes through the established homeploy workflow, not that file.

## Security notes

- Job IDs are bearer tokens. With no sessions or passwords, anyone holding a job ID can fetch that job's output and original inputs — do not treat the service as private storage for sensitive photos.
- User tokens are self-typed strings, not authentication; clearing browser storage gets a new quota bucket. Upload MIME comes from the declared `Content-Type` with no image decoder check, and size is capped at 5 MiB.
- With `TURNSTILE_SECRET` set, verification fails closed: missing token, wrong action (`edit`) or a hostname outside `TURNSTILE_HOSTNAMES` is a 403. An empty allowlist rejects everything; the Turnstile dashboard domain list is separate and must cover every hostname you test from.
- Keep `.env`, `api/credits.db` and `api/storage/` out of git, point storage at a path the operator controls, and never log keys.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
scripts/check.sh
```

`scripts/check.sh` is exactly what CI runs (`.github/workflows/ci.yml`, job `check`): mypy/ruff ratchet against `.ai/baseline.json`, architecture/layout check, anti-weakening guard, `pytest api`, `node --check web/app.js`, and `node tests/expired-card.cjs`. Green locally is the definition of done.

- Branch from `origin/dev` and target PRs at `dev`; GitHub's default base is `master`, the production pointer, deliberately behind. `.ai/baseline.json`, `pyproject.toml`, `.github/workflows/` and `CODEOWNERS` are protected and need the `config-change` label.
- Do not edit, skip or soften tests or checks to get green. `tests/frontend-smoke.js` is an interactive Playwright MCP script, not a Node CLI test; `docs/AI_WORKFLOW.md` has the loop and gates.

## Status and known gaps

- **No startup recovery.** Process death strands `running` jobs: they stay running forever, the credit is not refunded, and the sweep never touches them.
- **Single worker only.** Job slots and checkout throttles are in-process semaphores and dictionaries; multiple uvicorn workers multiply the 3-per-IP and 6-global limits instead of sharing them, and SQLite is not built for concurrent writers across processes.
- **Turnstile hostname allowlist.** Verification needs a non-empty `TURNSTILE_HOSTNAMES` and an exact match, listed both in the env var and the Turnstile dashboard; any mismatch fails closed with 403.
- **Artifacts are bearer-access by job ID.** No ownership check on `/api/result/{job_id}/file` or `/input/{n}`, so a leaked ID is a leaked photo.
- **Workers AI is unproven.** The free path was still broken at last check and has no automatic fallback; paid OpenAI is the working default. Retention is best-effort, deletion needs a running server with writable storage, and only known artifact names are removed.
- Verified, and not more: mocked route tests plus one real paid `POST /api/edit` → poll `done`, 1024×1024 PNG, credits 3 → 2. Nothing here claims output quality, provider uptime, free-tier viability or a dollar cost.

## Further reading

`AGENTS.md` (rules), `docs/ARCHITECTURE.md` (layers and file map), `docs/AI_WORKFLOW.md` (branch, PR, gates), `web/README.md` (frontend contract), `.env.example` (annotated settings).
