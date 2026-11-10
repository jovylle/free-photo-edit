# Free Photo Edit

Upload up to 5 photos with a Tagalog, English or Bisaya instruction; poll for an
AI-edited image, compare before/after, and download. FastAPI serves both the
static `web/` UI and same-origin `/api/*` routes. `photo-fusion/` is separate.

**Free to users does not mean free inference.** The current default is paid
OpenAI `gpt-image-2.5-sunburst` (1024×1024, medium). The earlier free-only
Workers AI plan was superseded by the paid-primary decision in FPE-2, with
`MAX_DAILY_EDITS=60` per rolling 24 hours. Historical probes estimated roughly
2.6¢/edit; that is not a current pricing guarantee or a dollar-denominated cap.
Workers AI remains selectable but its edit path was reported broken.

## Run locally (:8078)

From the project directory, using the existing virtual environment:

```bash
cd /Volumes/DevSSD/fore/lab/free-photo-edit
# First setup only: copy .env.example to .env if .env does not already exist.
# Fill secrets in .env, never in commands or browser code.
.venv/bin/python -m uvicorn api.server:app --host 127.0.0.1 --port 8078
```

Open `http://127.0.0.1:8078`. The runtime module is **`api.server:app`**;
`free_photo_edit_api` is a pytest-only alias created by `api/conftest.py`, not
an importable uvicorn package. The boot test exercises this entrypoint on an
unused port. Explicit `--port 8078` sets the port; `PORT` in `.env` alone does
not configure uvicorn.

For trusted LAN testing only, `--host 0.0.0.0` permits other devices to open
`http://<host-lan-ip>:8078`; do not port-forward it. Leave Turnstile secrets
blank only in isolated local development. Configured secrets enforce the bot
check and hostname/action validation.

## Endpoints

| Method | Path | Contract |
|---|---|---|
| `POST` | `/api/edit` | Multipart `image` (1–5 uploads), `prompt`, `preset`, `user`, and `cf-turnstile-response` when Turnstile is enabled. Returns **202** `{job_id, status: "running", credits_left, note}`; poll every ~10s. |
| `GET` | `/api/result/{job_id}` | `{job_id, status, preset, created_at, image_url, credits_left, artifacts_expired}`. Status remains `running`, `done` or `failed`; `image_url` is null until done or after expiry. Unknown job: 404. |
| `GET` | `/api/result/{job_id}/file` | Output PNG download; 410 after terminal-job expiry, otherwise 404 until done or if absent. |
| `GET` | `/api/result/{job_id}/input/{index}` | Original uploaded bytes (not a resized thumbnail), with JPEG/PNG/WebP MIME; 410 after terminal-job expiry, otherwise 404 if absent. |
| `GET` | `/api/jobs?user=` | Up to 30 newest jobs for history/resume. |
| `GET` | `/api/credits?user=` | `{user, credits_left}`. Read routes accept `X-User` or `?user=`; POST uses the multipart `user`. |
| `GET` | `/api/presets` | `background`, `clothes`, `restore`, `custom`. |
| `GET` | `/api/config` | `{turnstile_sitekey}` (null when unset); never exposes the secret. |

Validation: 1–5 nonempty JPEG/PNG/WebP uploads, each up to 5 MiB, prompt up to 2000
characters, known preset. Restore can supply its default instruction; custom
requires text. MIME is declared by the upload, not verified by an image decoder.

Errors: **400** validation, **402** no user credits, **403** bot check,
**429** rate/concurrency gates (with `Retry-After`), **503** global daily cap
(with `Retry-After`), **502** post-debit setup failure (refunded). After a 202,
engine failure is an HTTP-200 poll with `status: "failed"`, null image URL and
refunded credit, not a later HTTP 502.

User tokens and job IDs are not authentication. Anyone with a job ID can fetch
its files; do not treat this as private storage for sensitive photos.

## Keys and engines

**Image-model keys live server-side only.** `web/` fetches only same-origin
`/api/*`; it never calls model providers directly. The optional external
Turnstile widget uses a public sitekey, not an image-model secret.

Keep secrets in the gitignored `.env`; never log keys, put them in `web/`, or
commit `.env`, `api/credits.db` or storage. See `.env.example` for settings:

- `FREE_PHOTO_EDIT_ENGINE=openai` (default): `OPENAI_API_KEY`; multipart
  images/edits, `gpt-image-2.5-sunburst`, 1024×1024, medium. No `input_fidelity`.
- `FREE_PHOTO_EDIT_ENGINE=workers-ai`: `CF_ACCOUNT_ID` and
  `CLOUDFLARE_API_TOKEN` with Workers AI permissions. Default model
  `@cf/black-forest-labs/flux-2-dev` (override `WORKERS_AI_MODEL`), multipart
  `image`, `prompt`, `width=512`, `height=512`, `steps=25`.
  This is an explicit alternative, **not automatic failover**.
- `FREE_PHOTO_EDIT_ENGINE=pollinations`: unconfigured stub; engine raises
  `501 not-configured`, which the async route handles as failed/refunded.

Historical spike: schnell was text-to-image only; the planned SD img2img
fallback reportedly 404ed; SDXL/inpainting rejected image inputs; flux-2-dev
reportedly returned 500 on image input. These are historical findings, not a
fresh successful Workers AI round-trip. A successful free-quota edit and
neuron usage have **not** been verified by FPE-5.

`api/probe_live.py` makes one real provider call using the selected engine
(default **paid OpenAI**). It bypasses routes, credit accounting and the global
cap; it is **not** a route E2E test. Do not run it expecting a free probe.

Live route E2E (FPE-5, 2026-11-04): exactly one paid-primary `POST /api/edit`
(custom preset, 512×512 JPEG in) → **202** → poll `done`, 1024×1024 PNG out
(~963 KB), credits **3→2** for user `fpe5-live1` (job `24bb39c247c8`). Single
inference call, no retry. The route captures no provider usage or dollar cost —
actual spend is visible only in the OpenAI dashboard; the historical ~2.6¢/edit
estimate is not a cap, and token billing has no enforceable pre-request ceiling.

## Quota and concurrency

- `FREE_CREDITS=3`: initial credits per user token, **not a daily refill**.
  One accepted edit debits one; engine/setup failure refunds it.
- Debit, job insert and authoritative global-window check share one SQLite
  `BEGIN IMMEDIATE` transaction. Zero balance gives 402 without an engine call.
- `IP_MINUTE_LIMIT=3`: rolling minute per IP → 429.
- `IP_DAILY_LIMIT=24`: rolling 24 hours per IP + User-Agent hash → 429.
  User-Agent is spoofable; this is a generosity gate, not identity verification.
- `MAX_DAILY_EDITS=60`: rolling 24 hours across all recorded jobs, including
  failed jobs → 503. This limits accepted job count, not actual dollar charges.
- Three concurrent jobs **per IP**, shared across user tokens, six global.
  Semaphores are in-process; run one uvicorn worker for these limits.
- Process death can strand running jobs; restart recovery is not implemented.

## Storage / automatic TTL cleanup

Defaults: ledger `api/credits.db`, files `api/storage/<job_id>/input_0.*` and
`output.png`. Override with `FREE_PHOTO_EDIT_DB` / `FREE_PHOTO_EDIT_STORAGE`.
Relative override paths resolve from the server working directory.

Artifacts expire **seven days after the job first becomes `done` or `failed`**,
at `terminal_at + 604800` (inclusive). Directory/file mtime and submission time
do not determine expiry. Existing terminal rows with unknown completion times
get `terminal_at` set once at migration, granting seven days of grace; existing
running rows stay running with no terminal timestamp.

`api/cleanup.py` removes only regular `input_0.jpg`, `input_0.jpeg`,
`input_0.png`, `input_0.webp` and `output.png` files of expired ledger-backed
terminal jobs. Empty job directories are then removed. Running/unknown-status
jobs, orphan directories, unknown content, nested directories and symlinks are
left alone. Descriptor-relative no-follow directory access prevents deletion
through job/root symlinks. Failed file deletions are logged and retried; repeat
sweeps are safe. The configured storage parent path must be operator-controlled.
No job rows, credit balances, refunds, prompts, or quota history are pruned.

FastAPI's lifespan in `api/server.py` runs a sweep at startup, then every hour
in one background thread, stopped/joined on shutdown. No cron or new dependency
is needed. Physical cleanup requires the server to run and storage permissions
to permit deletion: expiry is not an exact-time erasure guarantee. Stranded
running jobs are deliberately never swept (restart recovery remains separate).

Expiry is enforced by time even before physical cleanup succeeds: poll/history
remain HTTP 200 with the original terminal status, `artifacts_expired: true`,
null `image_url`, and empty history `inputs`. Both artifact routes return **410
Gone**, not a missing-file error. The UI labels expired history and hides
comparison/download controls. Unexpected missing output *before* expiry still
returns the existing poll 500; it is not silently classified as TTL expiry.

This implementation is verified only with temporary storage/SQLite. No live
photos were deleted and no service was deployed/restarted. Production rollout
and deletion still require explicit approval; starting this updated server
against a real ledger/storage enables automatic cleanup after any legacy grace.

## Verification

```bash
scripts/check.sh          # everything CI enforces, one command
```

Setup once: `python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt`.
Rules for agents and humans: `AGENTS.md`, architecture: `docs/ARCHITECTURE.md`,
workflow: `docs/AI_WORKFLOW.md`.

TTL follow-up local run: **85 passed**, one existing Starlette/httpx
deprecation warning; expired-card DOM tests pass for done/failed jobs. TTL tests
cover boundary/repeated sweeps, migration grace, retry after deletion failure,
running/recent/unknown/symlink preservation, ledger/credit/quota preservation,
expired APIs before/after sweep, and startup/hourly-worker shutdown lifecycle.
Mocked route tests exercise 202 → poll → download with
credits 3 → 2, 402 with zero engine calls, failure/refund, 3/IP + 6-global
slots, validation and atomic daily-cap rejection. Engine tests use mocked
HTTP; they do not establish provider availability or image quality.

`tests/frontend-smoke.js` is a Playwright MCP function (not a Node CLI test).
FPE-4 verified mocked mobile workflows; see `docs/memories.md` for invocation.
The UI includes an accessible before/after range control, download, history,
reload persistence, light/dark theme, GCash/Ko-fi and a labeled ad placeholder.

**Live E2E pending:** this card permits one free Workers AI call, while FPE-2
accepted a paid-primary design with a documented broken free alternative.
No inference call was made in FPE-5 pending reconciliation of that requirement;
no neurons, paid cost or live credit decrement are claimed.

## Deployment boundary

Tunnel hostname: **`freephoto.uft1.com`** → origin `127.0.0.1:8078`.
Existing deployment notes identify launchd `com.fore.free-photo-edit`, staged
copy `~/fore/services/free-photo-edit/app` (launchd cannot read the lab volume),
staged `.env` secrets, `sync-live.sh`, and logs under
`~/Library/Logs/fore/free-photo-edit.{log,err}`. These notes are not a fresh
health check or approval to deploy.

FPE-5 does not sync/restart the live app, modify tunnel configuration, add a
`PROJECTS.md` row, or change dashboard inventory. The orchestrator owns deploy
and public verification; deploy via the established homeploy workflow.
