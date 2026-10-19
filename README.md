# Free Photo Edit

Upload 1 photo + instruction (Tagalog/English/Bisaya) → AI-edited photo back.
3 free edits per user, zero paid burn. Sibling of `photo-fusion/` (untouched).

## Backend

FastAPI server in `api/`. The browser never sees any image-model key — all AI
calls are server-side via OpenAI images/edits (default,
`gpt-image-2.5-sunburst`, ~2.6¢/edit) or Cloudflare Workers AI
(`FREE_PHOTO_EDIT_ENGINE` switch).

### Run

```bash
cd free-photo-edit
cp .env.example .env        # fill in keys only in .env (gitignored)
.venv/bin/python -m uvicorn api.server:app --host 127.0.0.1 --port 8078
```

> Run from `free-photo-edit/` so the `api` package resolves. Tests import the
> same code as `free_photo_edit_api` via the `api/conftest.py` bootstrap (the
> hyphenated folder name is not a valid Python identifier, so pytest synthesizes
> that name — it does **not** exist for uvicorn).
>
> LAN access (phones/other devices): start with `--host 0.0.0.0` instead, then
> open `http://<mac-lan-ip>:8078` (e.g. `http://192.168.1.47:8078`). LAN only —
> never port-forward this; public deploy is the separate tunnel step.

### Endpoints

| Method | Path | Notes |
|---|---|---|
| `POST` | `/api/edit` | multipart: `image` (×1), `prompt`, `preset`, `user`. Returns **202** `{job_id, status: running, credits_left}` immediately; poll `GET /api/result/{id}` every ~10s. 402 when out of credits / IP cap, 400 validation, 429 when per-user (3) or global (6) slots are full (has `Retry-After`). |
| `GET` | `/api/result/{job_id}` | `{job_id, status, preset, created_at, image_url, credits_left}` (image_url null until done). |
| `GET` | `/api/result/{job_id}/file` | The output PNG (404 until done). |
| `GET` | `/api/result/{job_id}/input/0` | Stored input thumb (jpeg/png/webp MIME). |
| `GET` | `/api/jobs?user=` | Last 30 jobs newest-first. Powers UI history + resume-after-tab-close. |
| `GET` | `/api/credits?user=` | `{user, credits_left}`. Identify via `X-User` header or `?user=` query. |
| `GET` | `/api/presets` | 4 presets: background / clothes / restore / custom. |

### Key rule

**Image-model keys live server-side only.** The browser never calls OpenAI,
Workers AI, or Pollinations directly — `web/` talks only to `/api/*` on the
same origin. `OPENAI_API_KEY` (primary) and `CLOUDFLARE_API_TOKEN` +
`CF_ACCOUNT_ID` (fallback) are backend env/secrets; never put them in `web/`,
never commit `.env`, never log keys.

### Modules

- `api/server.py` — routes, validation, async edit (202 + background thread),
  3-per-user + 6-global slot semaphores, `/api/jobs` history, input-thumb route.
- `api/prompts.py` — preset map + `resolve_preset` + `build_edit_prompt`: user
  instruction quoted verbatim, face/identity invariants repeated.
- `api/credits.py` — SQLite ledger (`users`, `jobs`). `FREE_CREDITS=3`.
  Check-then-debit in one `BEGIN IMMEDIATE`; 0-credit → 402 with **zero**
  upstream calls; engine failures refund.
- `api/engines.py` — `Engine.edit(input_bytes, prompt) -> png_bytes`.
  `OpenAiEngine` (primary): images/edits multipart to
  `gpt-image-2.5-sunburst`, 1024x1024 medium, ~2.6¢/edit, key `OPENAI_API_KEY`
  (never sends `input_fidelity` — sunburst 400s on it); `WorkersAiEngine`
  (fallback, live edit path broken → `FREE_PHOTO_EDIT_ENGINE=workers-ai`);
  `PollinationsEngine` stub → 501 `not-configured`.

### Engine spike — history (2026-10-11)

> 2026-10-17: default engine is now OpenAI `gpt-image-2.5-sunburst`
> (~2.6¢/edit, 1024px, fidelity built-in). `gpt-image-1-mini` repainted faces
> and garbled text (fidelity locked low); 1.5+high (9.1¢) zoom-cropped the
> subject. flux-2-dev 500s on image input; the Workers AI
> path stays selectable but its edit path is unusable. Probe:
> `.venv/bin/python api/probe_live.py` (reads `.env`, 1 real edit).

- schnell is **text-to-image only** — unusable for edits. SDXL-base's live
  graph rejects `image_b64` (400 code 3030); SD-inpainting demands `mask_image`
  then rejects valid uploads (500 decode error). Default is now flux-2-dev
  (documented image inputs, multipart, 512px, ~18–37 neurons/run ≈ 260+/day
  on the free pool).
- Auth: the wrangler `CLOUDFLARE_API_TOKEN` gets HTTP 401 on `/ai/run` — use a
  token with Workers AI Read+Edit (dashboard AI > Workers AI > Use REST API).
  Probe: `CLOUDFLARE_API_TOKEN='<token>' .venv/bin/python api/probe_live.py`
  (1 call, ~a few dozen neurons). If schnell ever gains img2img, set
  `WORKERS_AI_MODEL=@cf/black-forest-labs/flux-1-schnell`.

### Quota (public-launch rules, 2026-10-18)

- 3 credits per user id (`FREE_CREDITS`), 1 credit = 1 edit.
- Turnstile bot check on every edit once `TURNSTILE_SECRET` is set
  (fail closed; off in dev when unset). Widget key via `GET /api/config`.
- Rolling windows, all pre-debit (rejections cost zero): per-IP 3/minute
  (`IP_MINUTE_LIMIT`) → 429; per-device (IP + UA hash, `IP_DAILY_LIMIT`,
  default 24/day so friends behind one home WiFi each get their own
  bucket — UA spoofable, generosity cap only) → 429; global 60/day
  (`MAX_DAILY_EDITS`, ~$1.56 worst case) → 503.
- 3 concurrent jobs per user + 6 global → 429 + `Retry-After`.

### Storage TTL sweep

Outputs live under `api/storage/<jobid>/` (input thumb + output.png).
Prune anything older than 7 days:

```bash
find api/storage -mindepth 1 -maxdepth 1 -mtime +7 -exec rm -rf {} +
```

### Tests

```bash
.venv/bin/python -m pytest api -v
```

`test_edit_contract.py` (202 + poll-until-done, credit decrement, mocked
engine), `test_async_queue.py` (running→done, 3-per-user + 6-global 429,
failure refund+failed), `test_abuse.py` (Turnstile gate incl. unit-verified
siteverify accept/reject, IP minute/daily 429s, global 503, `/api/config`),
`test_credits.py` (ledger: 0-credit, double-submit,
3-cap, refund, IP count), `test_engines.py` (mocked OpenAI + Workers AI,
engine switch, 501 stub, prompt cases).

### Frontend

`web/index.html` + `web/app.js` — one card: upload box, prompt box with
edit-type chips underneath (chip fills an empty box; typed text always wins),
one Edit button. Each result shows the original photo(s) with the edited
image below, plus Download. On submit the form collapses to an "edit another" bar
(photo + prompt kept for retry) and the newest result stages under the
button; history hides in a native `<details>` expander, donate + ad slot stay
at the bottom. Reload-proof: prompt + chip in localStorage, photo blob in
IndexedDB, latest done result re-stages on load. Donate QR hidden behind a
tap-to-show expander; Ko-fi link stays visible. Same-origin `/api/*` only.

Theme: light by default, dark via the header toggle (saved in localStorage
`fpe-theme`). Colors live in `web/site.css` tokens. Static pages:
`about.html`, `privacy.html`, `terms.html` (shared footer, no build step).

### Deploy (live 2026-10-18)

- Public: **https://freephoto.uft1.com** (tunnel `mac-mini-local`).
- Origin: staged copy at `~/fore/services/free-photo-edit/app` (NOT the lab
  dir — launchd can't read `/Volumes/DevSSD`), venv inside, secrets in
  staged `.env` (600, never rsynced). Sync after every change:
  `./sync-live.sh` (repo → staged, restarts service, checks local+public).
- Service: launchd `com.fore.free-photo-edit` (KeepAlive, 127.0.0.1:8078).
  Logs: `~/Library/Logs/fore/free-photo-edit.{log,err}`.
- Tunnel: hostname in `mac-mini-infra/cloudflared/config.yml` → copy to
  `~/.cloudflared/config.yml` → `cloudflared tunnel route dns mac-mini-local
  freephoto.uft1.com` → `sudo launchctl kickstart -k system/com.jo.cloudflared`.
- Cloudflare: add a rate-limit rule on `freephoto.uft1.com/api/*` + Bot Fight
  Mode (dashboard, zone `uft1.com`).
- After the Turnstile widget exists: sitekey → staged `.env`
  (`TURNSTILE_SITEKEY`, `TURNSTILE_SECRET`,
  `TURNSTILE_HOSTNAMES=freephoto.uft1.com`) → kickstart service → verify a
  real browser edit + a no-token 403.
