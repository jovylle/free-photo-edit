# free-photo-edit — session memories

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

## Test hermetic lesson (2026-10-18)
- Lab `.env` holds the REAL Turnstile secret and `load_dotenv` pulls it into
  the pytest process — 13 tests 403'd. Fixtures now force
  `TURNSTILE_SECRET=""` (and SITEKEY in the config test). Any new
  env-gated behavior needs the same treatment in fixtures.

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
