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
