# PayMongo sandbox verification

Status: runbook for the remaining rollout steps. No live keys, no real money.

Do this only with **test-mode** keys (`sk_test_...`). Live mode is a separate
approval at the end of `payments-plan.md`.

## 0. What you need

- A PayMongo account with test mode activated.
- This worktree running locally (or the dev API host).
- A way to make the local API reachable from the internet for the webhook:
  `cloudflared tunnel --url http://127.0.0.1:8078` (no account needed), or the
  dev API behind the existing Cloudflare route.

## 1. Create test keys

PayMongo dashboard → Developers → API keys → **Test mode** → copy the secret
key (`sk_test_...`). Put it in `.env` (gitignored; never commit):

```
PAYMONGO_SECRET_KEY=sk_test_...
PAYMONGO_WEBHOOK_SECRET=whsk_...        # filled in step 3
PAYMENT_SUCCESS_URL=http://127.0.0.1:8078/?paid=1
PAYMENT_CANCEL_URL=http://127.0.0.1:8078/
```

`PAYMENT_SUCCESS_URL`/`CANCEL_URL` must be the **frontend** origin (the Pages
build if you are testing that, otherwise the local app). The webhook must be
the **API** origin.

## 2. Start an isolated API

Use a throwaway DB so sandbox credits never touch dev data:

```sh
FREE_PHOTO_EDIT_DB=/tmp/fpe-sandbox.db \
FREE_PHOTO_EDIT_STORAGE=/tmp/fpe-sandbox-storage \
.venv/bin/python -m uvicorn api.server:app --port 8078
```

Check it answers: `curl -s localhost:8078/api/packs` should list the three packs.

## 3. Register the webhook

1. Start the tunnel: `cloudflared tunnel --url http://127.0.0.1:8078`, copy the
   `https://<random>.trycloudflare.com` host.
2. PayMongo dashboard → Developers → Webhooks → **Create**:
   - URL: `https://<tunnel-host>/api/paymongo/webhook`
   - Event: **`checkout_session.payment.paid`** only
3. Copy the webhook's secret (`whsk_...`) into `PAYMONGO_WEBHOOK_SECRET` and
   restart the API.

A quick tunnel changes hostname on restart; re-point the webhook if you restart
it. For a repeatable test, use the deployed dev API instead.

## 4. Buy a pack

1. Open the app, click a pack. With the email field empty the button refuses —
   fill in any address (e.g. `sandbox@example.com`).
2. On the PayMongo page use a test card (`4343434343434345`, any future expiry,
   any CVC), or pick GCash/Maya/GrabPay and press **Authorize** on PayMongo's
   simulator (no real wallet needed).
3. **Do not** return to the success URL — close the tab instead. The webhook
   must still grant the credits:

```sh
curl -s "localhost:8078/api/credits?user=<the browser's freephotoedit_user>"
```

Find that token in the browser's localStorage, or watch the API log for the
webhook POST. Balance should be 3 free + the pack's edits.

Reopen the app with `?paid=1` to see the "Confirming your payment…" poll end in
"Credits added".

## 5. Duplicate and forged deliveries

Use the real session id (returned by `POST /api/checkout`, also `session_id` in
the browser network tab) and the pack price in centavos (Starter = `4900`):

```sh
export PAYMONGO_WEBHOOK_SECRET=whsk_...
scripts/paymongo_webhook_test.py --session cs_... --amount 4900 --mode duplicate
scripts/paymongo_webhook_test.py --session cs_... --amount 4900 --mode forged
scripts/paymongo_webhook_test.py --session cs_... --amount 4900 --mode stale
scripts/paymongo_webhook_test.py --session cs_... --amount 4900 --mode low
```

| Mode | Expected |
| --- | --- |
| `valid` | 200, `credited` > 0 (use a fresh, unpaid session) |
| `duplicate` | 200 then 200, second `credited` is 0, balance unchanged |
| `forged` | 401, no credit |
| `stale` | 401, no credit |
| `low` | 400, no credit |

The script exits non-zero on any mismatch, so it is safe for a release check.
`--url` points at a deployed dev API when you are not local.

## 6. Confirm the ledger

```sh
sqlite3 /tmp/fpe-sandbox.db \
  "SELECT session_id, user, pack, credits, amount, status, email, paid_at FROM payments;"
```

A paid session has `status='paid'`, a non-null `paid_at`, and the user's row has
`paid_total` equal to the credits bought once. Duplicate deliveries must not
change either number.

## 7. Reset

Delete `/tmp/fpe-sandbox.db` and `/tmp/fpe-sandbox-storage`. Remove the tunnel's
webhook in the PayMongo dashboard. Leave `.env` on test keys until live mode is
approved; never put live keys in the dev environment.

## Known limits

- Checkout rate limiting is per API process and per hour (5); restarting resets it.
- Abandoned sessions stay `pending` forever; that is harmless and no cleanup job
  exists.
- The buyer's identity is still the localStorage token plus the checkout email;
  the email is for manual recovery, not login.
