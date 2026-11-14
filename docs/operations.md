# Operations

One page: what runs where, how it is deployed, and what to check when it breaks.

## Topology

Exactly one backend: **this Mac**.

- API + static UI: `uvicorn api.server:app` on `127.0.0.1:8078`, kept alive by the
  LaunchAgent `com.fore.free-photo-edit` → `~/fore/services/free-photo-edit/run-free-photo-edit.sh`.
- Live code: `~/fore/services/free-photo-edit/app` (its own git repo), rsynced from
  `/Volumes/DevSSD/fore/lab/free-photo-edit` by `sync-live.sh`. That rsync excludes
  `.env`, `api/credits.db*` and `api/storage/` — data never leaves the box.
- Data: `app/api/credits.db` (sqlite) and `app/api/storage/` (photos, purged after 7 days).
- Public URL: `https://freephoto.uft1.com` via the `mac-mini-local` cloudflared tunnel
  (id `1c23d677-…`), ingress to `localhost:8078`.
- Logs: `~/Library/Logs/fore/free-photo-edit.log` and `.err`.
- Backups: LaunchAgent `com.fore.free-photo-edit-backup`, daily 03:30 → `backups/<date>-daily`.

The power settings are part of the deployment: `sleep 0`, `disksleep 0`,
`autorestart 1`, `womp 1`, on AC. If the Mac reboots or loses power, launchd brings the
service back; a home-internet or tunnel outage is a visible outage with no failover.

## Deploy

```bash
cd /Volumes/DevSSD/fore/lab/free-photo-edit
bash scripts/check.sh          # tests + ratchets; never deploy on red
bash sync-live.sh              # rsync -> live copy -> launchctl kickstart -> curl checks
```

`sync-live.sh` restarts the process, so **do not run it while a job is running** — a
restart kills in-flight edits. Check first:

```bash
sqlite3 ~/fore/services/free-photo-edit/app/api/credits.db \
  "select count(*) from jobs where status not in ('done','failed');"
```

Health: `curl -fsS https://freephoto.uft1.com/api/config` returns the revision and the
Turnstile sitekey.

## Cache

Cloudflare caches `app.js`, `site.js` and `site.css` for 4h, and the zone's purge API
rejects this token (`code 10000`), so **bump the version query** in all HTML files
whenever one of those files changes — otherwise users keep the old file for hours.
`index.html` is served `no-cache`.

## Dev / staging

`free-photo-edit-dev.pages.dev` is **static files only** (Cloudflare Pages,
`scripts/deploy-dev-frontend.sh`) pointed at the *production* API. Prod allows that
origin via CORS, and its hostname is in the Turnstile widget's domain list. Use it for
frontend and visual checks; it cannot validate backend changes, and an edit made there
writes to the production database. See ADR 0003.

## Retired

The old homeploy stack on `elitedesk` (container `free-photo-edit-dev`, hostnames
`free-photo-edit-dev.uft1.com` and `free-photo-edit.uft1.com`, shared tunnel
`9ced8b06-…`) is retired: the container is stopped, both DNS records are deleted, and
the original records plus `deploy.yml` are kept in `docs/retired/`. Those hostnames now
fall through the `*.uft1.com` wildcard and return a Cloudflare error page — expected.
