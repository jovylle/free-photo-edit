# Xendit sandbox verification

Status: runbook for the remaining rollout steps. Test mode only — no live keys,
no real money.

## 0. What you need

- A Xendit account in **Test Mode** (active at signup; live needs business KYC).
- This worktree running locally.
- `cloudflared` (already installed on this machine).

## 1. Keys

1. Dashboard → **Settings → Developers → API keys → Generate secret key**.
   2FA is required; the key (`xnd_development_...`) is shown **once**.
2. Same page → **View Webhook Verification Token** (account password + 2FA).

Put both in `.env` (gitignored; never commit):

```
XENDIT_SECRET_KEY=xnd_development_...
XENDIT_WEBHOOK_TOKEN=...
PAYMENT_SUCCESS_URL=https://<tunnel-host>/?paid=1
PAYMENT_CANCEL_URL=https://<tunnel-host>/
```

Xendit requires **HTTPS** return URLs, so `http://127.0.0.1` is rejected. Use
the tunnel host for the frontend as well as the webhook.

## 2. Start an isolated API

Use a throwaway DB so sandbox credits never touch dev data:

```sh
FREE_PHOTO_EDIT_DB=/tmp/fpe-sandbox.db \
FREE_PHOTO_EDIT_STORAGE=/tmp/fpe-sandbox-storage \
.venv/bin/python -m uvicorn api.server:app --port 8078
```

`curl -s localhost:8078/api/packs` should list the three packs.

## 3. Tunnel

```sh
cloudflared tunnel --url http://127.0.0.1:8078
```

Copy the `https://<random>.trycloudflare.com` host, put it in the two return
URLs above, and restart the API so it picks them up.

## 4. Register the webhook

Dashboard → **Settings → Developers → Webhooks** → the **Payment Session**
event → URL `https://<tunnel-host>/api/xendit/webhook` → Test and save.

The probe returns `200` even for an unknown session id, so the save should pass.
A bad or missing `x-callback-token` returns `401`.

Two things that will waste an hour otherwise:

- Xendit's URL validator rejects `*.trycloudflare.com` quick tunnels ("may
  contain private or local IPs"). Use a real hostname — the dev API
  (`https://free-photo-edit-dev.uft1.com/api/xendit/webhook`) is the one that
  works today.
- Cloudflare blocks `Python-urllib` at the edge (error 1010), so the helper
  script sends its own User-Agent. Keep it when editing that script.

## 5. Buy a pack

1. Open the tunnel URL, click a pack, fill in an email.
2. Pay with test card `4000 0000 0000 1000`, any future expiry, any CVV
   (3DS frictionless). The hosted page must read **₱49.00** — verified once on
   2026-11-11, re-check if a price changes. If it reads ₱0.49 the amount unit
   is centavos: make `_xendit_amount` return `centavos` unchanged, restart,
   and repeat this step.
3. **Close the tab without returning.** The webhook must still grant credits:

```sh
curl -s "localhost:8078/api/credits?user=<the browser's freephotoedit_user>"
```

Find the token in the browser's localStorage, or watch the API log for the
webhook POST. Balance should be 3 free + the pack's edits.

Reopen the app with `?paid=1` to see "Confirming your payment…" end in
"Credits added".

## 6. Duplicate and forged deliveries

Create a session from the app (click a pack) but **do not pay it** — that is the
`pending` row the helper needs. The amount is what the server sent to Xendit
(Starter = `49`; see the `payments` row if unsure).

```sh
export XENDIT_WEBHOOK_TOKEN=...
scripts/xendit_webhook_test.py --session ps_... --amount 49 --mode valid
scripts/xendit_webhook_test.py --session ps_... --amount 49 --mode duplicate
scripts/xendit_webhook_test.py --session ps_... --amount 49 --mode forged
scripts/xendit_webhook_test.py --session ps_... --amount 48 --mode low
scripts/xendit_webhook_test.py --session ps_x --amount 49 --mode unknown
```

| Mode | Expected |
| --- | --- |
| `valid` | 200, `credited` > 0 (fresh, unpaid session) |
| `duplicate` | 200 then 200, second `credited` is 0, balance unchanged |
| `forged` | 401, no credit |
| `low` | 400, no credit |
| `unknown` | 200, `credited` 0 (no retry storm) |

The script exits non-zero on any mismatch, so it is safe as a release check.
`--url` points at a deployed dev API when you are not local.

## 7. Confirm the ledger

```sh
sqlite3 /tmp/fpe-sandbox.db \
  "SELECT session_id, user, pack, credits, amount, status, email, paid_at FROM payments;"
```

A paid session has `status='paid'`, a non-null `paid_at`, and the user's row has
`paid_total` equal to the credits bought once. Duplicate deliveries must not
change either number.

## 8. Reset

Delete `/tmp/fpe-sandbox.db` and `/tmp/fpe-sandbox-storage`. Remove the tunnel's
webhook in the Xendit dashboard. Leave `.env` on test keys until live mode is
approved; never put live keys in the dev environment.

## Known limits

- Checkout rate limiting is per API process and per hour (5); restarting resets it.
- A quick tunnel changes hostname on restart; re-point the webhook and the two
  return URLs if you restart it. The deployed dev API avoids this.
- The webhook token is static (no timestamp). Replays are harmless only because
  a duplicate delivery is a no-op.
