# free-photo-edit — BUILD BRIEF (portable handoff)

> For a fresh agent with zero chat context. Everything to build is in this file.
> Lab root: `/Volumes/DevSSD/fore/lab`. Project folder: `free-photo-edit/` (sibling of `photo-fusion/`).
> Status 2026-10-11: plan-approved, **zero code written** (`free-photo-edit/` contains only this file).
> Parent specs: `.hermes/plans/2026-10-11_free-photo-edit-design.md` (design C = A now, C-ready)
> + `.hermes/plans/2026-10-11_free-photo-edit-implementation.md` (decisions + files + order).
> Leave `photo-fusion/` untouched.

## 0. Start here (ordered)

1. Read this brief fully, then the 2 plans above + `photo-fusion/README.md` (endpoint + module pattern).
2. Check kanban: `hermes kanban --board free-photo-edit list`. Tasks FPE-1..FPE-5 exist (ids §6).
   **Known issue:** FPE-1 (`t_ad3c1870`) is `blocked` — kanban runner crashed ×2
   (`ModuleNotFoundError: hermes_cli`, 2026-10-11 20:38). Recover with
   `hermes kanban --board free-photo-edit retry t_ad3c1870` or
   `hermes kanban --board free-photo-edit reset t_ad3c1870 --status todo`.
   If the runner is still broken, build natively in order FPE-1→FPE-5 and skip kanban.
3. Bootstrap venv per task (§7), TDD each task (§5), never commit/push.
4. After all green: capture an mcpnotes note, do NOT deploy (deploy = orchestrator job, §8).

## 1. Goal (one sentence)

Build `free-photo-edit/` — self-serve 1-photo-edit site for PH FB mobile users:
upload 1 photo + instruction (Tagalog/English/Bisaya) → AI edit back,
3 free/day/IP, zero paid burn.

## 2. Locked decisions (do not relitigate)

- **Runtime = FastAPI + static `web/`** (copy `photo-fusion/` pattern). "Workers AI"
  means the Cloudflare AI **HTTP API called server-side**, NOT a Cloudflare Worker
  runtime. Local run `uvicorn api.server:app --port 8078`,
  tunnel hostname `freephoto.uft1.com` (deploy-time only).
- **Ledger = SQLite file** `free-photo-edit/api/credits.db`, tables `users` + `jobs`
  (same shape as `photo-fusion/api/credits.py:20-60`). No shared-D1 work in v1
  (D1 prefix `free_photo_edit_` reserved for later).
- **Storage = `free-photo-edit/api/storage/<jobid>/output.png`** (match photo-fusion
  `PHOTOFUSION_STORAGE` pattern; impl plan's `storage/` shorthand means this).
  Input thumbs alongside. Short-TTL sweep in FPE-5. R2 is OUT for v1.
- **Async = 202 + background thread + poll**: `POST /api/edit` → `202 {job_id}`
  instantly; worker thread runs engine; UI polls `GET /api/result/{id}` every ~10s.
  Slot caps 3-per-IP + 6-global → `429 + Retry-After` (copy
  `photo-fusion/api/server.py:80-120` semaphore pattern).
- **Identity + quota = 3/day/IP.** Identify via `?user=` query or `X-User` header
  (browser uses query, same as photo-fusion `GET /api/credits`), IP via
  `CF-Connecting-IP` → `X-Forwarded-For` → socket (copy `_client_ip` in
  `photo-fusion/api/server.py:60-80`). `BEGIN IMMEDIATE` check-then-debit,
  `1 credit = 1 job`. 0-credit → `402` with **zero** upstream calls.
  Engine failure → refund + `failed` status.
- **Keys server-side only.** `web/` talks to same-origin `/api/*` only.
  Never put `CLOUDFLARE_API_TOKEN` / Pollinations key in `web/`,
  never commit `.env` or `api/credits.db*` or `api/storage/`.

## 3. Engines (free only)

| Engine | Model / route | Quota | Key (backend env only) | Role |
|---|---|---|---|---|
| Primary (build now) | Workers AI `@cf/black-forest-labs/flux-1-schnell`, 512px img2img | 10k neurons/day ≈ ~2,000 edits/day @512px (4.8/ tile), resets 00:00 UTC, no card | `CF_ACCOUNT_ID` + `CLOUDFLARE_API_TOKEN` | Day-one engine |
| Fallback (stub only) | Pollinations Flux (0 Pollen) | daily Pollen grants, expire daily, watermark, anon = 401 | free Seed key `sk_/pk_` | `PollinationsEngine` returns `501 not-configured` in v1 |
| OUT | Gemini image (free tier removed Aug 2026), Codex/Go login (not free) | — | — | Do not build on |

Sources: `free-tier-tools.md:41-53`.

**Spike outcome (2026-10-11, verified):** schnell is text-to-image ONLY — no
image input. Default engine is now `@cf/runwayml/stable-diffusion-v1-5-inpainting`
(img2img `image_b64` + strength 0.8 + 20 steps, mask omitted, 512px, same neurons
pool); the plan's named `@cf/runwayml/stable-diffusion-v1-5-img2img` 404s and
SDXL-base's live graph rejects `image_b64` (400 code 3030). Token needs
Workers AI Read+Edit (wrangler token → 401). Probe: `api/probe_live.py`.

## 4. What to build (copy-adapt photo-fusion 1–3 photos → 1 photo)

Copy these, slim to 1 photo (photo-fusion allows 1–3, `MAX_UPLOADS=3` → `1`):

- `free-photo-edit/api/__init__.py` (empty) + `free-photo-edit/api/conftest.py`
  — package `free_photo_edit_api`, same bootstrap trick as
  `photo-fusion/api/conftest.py:1-24` (importlib `_load`, `__path__` override,
  preload `prompts,credits,engines,server`). Run pytest/uvicorn from `free-photo-edit/`.
  Note: photo-fusion also symlinks `photo_fusion_api -> api`; replicate
  (`free_photo_edit_api -> api`) or rely on conftest — pick one, note it.
- `api/credits.py` — copy `photo-fusion/api/credits.py` ledger, `FREE_CREDITS=3`
  (env-overridable). Keep `BEGIN IMMEDIATE` check-then-debit + `refund()`.
  Drop photo-fusion's admin-key/IP-layer extras; keep `users` + `jobs` tables.
- `api/engines.py` — `Engine.edit(input_bytes, prompt) -> png_bytes` interface
  (photo-fusion uses `generate(job_dir, uploads, prompt, model)` — adapt to the
  simpler 1-photo signature). `WorkersAiEngine` real (REST, §3). `PollinationsEngine`
  stub → `501 not-configured`. Paid-lane stub same pattern.
- `api/prompts.py` — copy structure of `photo-fusion/api/prompts.py:1-60`
  (preset map + `INVARIANTS` + `resolve_preset`). 4 presets: `background`,
  `clothes`, `restore`, `custom`. Preset = prompt template, user instruction
  quoted verbatim, preserve-face/identity invariants.
- `api/server.py` — copy `photo-fusion/api/server.py:1-120` shape, rename
  `POST /api/fuse` → **`POST /api/edit`** (multipart `image`×1 jpeg/png/webp ≤5MB,
  `prompt` ≤2000 chars, `preset`, `user`). Contract table §5. Background thread,
  slot semaphores, `api/storage/` outputs, `/api/jobs?user=` history (newest-first,
  resume polling), input-thumb route.
- `web/index.html` + `web/app.js` — copy-adapt `photo-fusion/web/index.html` +
  `app.js`: 1-photo dropzone, 3 preset buttons + custom box (TL/EN/Bisaya
  placeholder), 202 + 10s poll, before/after slider, download btn,
  `credits left: N` badge, `/api/jobs` resume on load, donate (GCash/Ko-fi) + 1 ad slot.
  Same-origin `/api/*` only.
- `.env.example` (`CF_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN`, `FREE_CREDITS=3`,
  `PORT=8078`, `FREE_PHOTO_EDIT_DB=./api/credits.db`,
  `FREE_PHOTO_EDIT_STORAGE=./api/storage`) — cf. `photo-fusion/.env.example`.
- `.gitignore` (`.env`, `.venv/`, `__pycache__/`, `.pytest_cache/`,
  `api/credits.db*`, `api/storage/`) — cf. `photo-fusion/.gitignore`.
- `README.md` — run + endpoints + key rule (mirror `photo-fusion/README.md:9-40`).
- Out of scope: login, text-to-image, gallery, paid unlocks, HEIC convert (400 in v1).

## 5. Endpoint contract (exact shapes)

| Method | Path | Notes |
|---|---|---|
| `POST` | `/api/edit` | multipart `image`×1 + `prompt` + `preset` + `user` → `202 {job_id, status: running, credits_left}`; `400` bad MIME/oversize/>1 file; `402` out-of-credits (zero upstream); `429` slots full + `Retry-After` |
| `GET` | `/api/result/{id}` | `{job_id, status, preset, created_at, image_url, credits_left}` (`image_url` null until done) |
| `GET` | `/api/result/{id}/file` | output PNG (404 until done) |
| `GET` | `/api/result/{id}/input/0` | stored input thumb |
| `GET` | `/api/jobs?user=` | last 30 newest-first (history + resume) |
| `GET` | `/api/credits?user=` | `{user, credits_left}` (also `X-User` header) |

## 6. TDD + VERIFY per task (failing test → implement → green)

| Task | Kanban id | VERIFY (must go green before complete) |
|---|---|---|
| FPE-1 scaffold + ledger | `t_ad3c1870` (blocked — see §0.2) | `python3 -m pytest api/test_credits.py -v` (0-credit→402 zero engine calls, sequential + concurrent double-submit single-debit, 3/day cap) |
| FPE-2 engines + prompts | `t_0bd7a564` (parent FPE-1) | `python3 -m pytest api/test_engines.py -v` (mocked HTTP, 501 stub, prompt cases) + spike note (model id + params) in comment |
| FPE-3 routes 202+poll | `t_0a48525a` (parent FPE-2) | `python3 -m pytest api -v` (202 + poll-until-done, running→done, 3-parallel, 6-global 429, fail→refund; mocked engine only) |
| FPE-4 web UI | `t_2f126534` (parent FPE-3) | `python3 -m http.server 8901 --directory web & sleep 1; curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8901/; kill %1` → 200, mobile-first |
| FPE-5 live E2E + README | `t_82a72553` (parent FPE-4) | 1 live 512px probe on free quota + credit decrement (1 call only) + `pytest api -v` tail |

Board: `free-photo-edit`, workdir `/Volumes/DevSSD/fore/lab`, chained parents,
assignee `default`. Workers: fresh session per task, fat comment + FINAL SUMMARY
(files/test counts/deviations/artifacts) before `kanban_complete`, never commit/push.
Full task bodies: `hermes kanban --board free-photo-edit show <id>`.

## 7. Env bootstrap (each task)

```bash
cd /Volumes/DevSSD/fore/lab/free-photo-edit
python3 -m venv .venv 2>/dev/null; .venv/bin/python -m pip install fastapi uvicorn httpx python-dotenv pytest 2>&1 | tail -2
cp -n .env.example .env  # fill keys only in .env (gitignored), never log keys
.venv/bin/python -m pytest api -v
.venv/bin/python -m uvicorn api.server:app --host 127.0.0.1 --port 8078
```

Reference: `photo-fusion/.venv/bin/` has fastapi/httpx/pytest/python-dotenv (no pip
inside — use `python -m pip` or system pip if needed). No new deps beyond these.

## 8. Deploy (only when all green — NOT part of build)

1. Tunnel: add `freephoto.uft1.com → 127.0.0.1:8078` to
   `mac-mini-infra/cloudflared/config.yml`, sync to `~/.cloudflared/`, restart tunnel.
2. Then: PROJECTS.md row (cf. `PROJECTS.md:92` photo-fusion row) + jov-dash tile
   (anti-forget: `~/fore/services/dash/config.json` or dash UI).
3. mcpnotes capture after meaningful work (title = short summary, tags = `free-photo-edit`).

## 9. Guardrails

- No `git add/commit/push` unless explicitly told. No new deps (§7 list only).
- Don't touch `photo-fusion/`. Secrets in `.env` only, never in `web/` or logs.
- Decision fork (e.g. engine model switch): pick best default, note + "veto me" in
  comment, keep going. Block only if truly stuck (missing creds, contradictory reqs).
- Plan-before-code (AGENTS.md) is satisfied — these plans are approved; build is authorized.
