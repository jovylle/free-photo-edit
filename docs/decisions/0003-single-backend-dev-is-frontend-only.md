# 0003 — One backend (this Mac); dev is a frontend-only staging surface

**Status** — accepted (2026-11-14)

**Context** — Dev used to be a full second deployment: a Docker container built by
homeploy on `elitedesk` (its own FastAPI + sqlite DB), reached through a separate
cloudflared tunnel at `free-photo-edit-dev.uft1.com`, with a third copy of the static
files on the Cloudflare Pages project. Nothing kept the container current, so it
drifted (still serving emoji preset labels two days after they were removed). Two
backends means two DBs, two deploy paths, and a staging surface that lies.

**Decision** — Production is the only backend: FastAPI on this Mac (launchd
`com.fore.free-photo-edit`, uvicorn 127.0.0.1:8078) behind the `mac-mini-local`
tunnel at `freephoto.uft1.com`, with sqlite + photo storage local to the box.
`free-photo-edit-dev.pages.dev` is static files only and calls the **production**
API cross-origin, permitted by `CORSMiddleware` with
`allow_origin_regex=https://([a-z0-9-]+\.)?free-photo-edit-dev\.pages\.dev`
(tested in `api/test_edit_contract.py`). The Turnstile widget (`0x4AAAAAAFZbe1GmHJK4jbXI`)
lists that hostname too. The elitedesk container is stopped.

**Consequences** — Frontend changes: deploy pages to dev, look, then `sync-live.sh`.
Backend changes: straight to prod, test there (dev cannot prove them). Dev has no
history/credits of its own — it is a fresh anonymous user per origin, and an edit made
from the dev URL spends real prod credits. Preview deployment URLs
(`<hash>.free-photo-edit-dev.pages.dev`) are not in the Turnstile allowlist, so verify
at the canonical project URL only.
