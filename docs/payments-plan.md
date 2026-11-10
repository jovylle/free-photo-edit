# Free Photo Edit: basic paid credit packs

Status: implemented on worktree 2026-11-10 (tests green, 107 passed); sandbox
runbook ready in `docs/payments-sandbox.md`. Sandbox and live rollout not done;
nothing committed, deployed or charged.

## Goal

Let a visitor buy a pack of edits with GCash, Maya, GrabPay or card, and have
those edits land in the existing credits ledger. Keep the current 3 free edits
as a trial. Payments are handled by PayMongo Hosted Checkout; the app never
sees card data.

## Decisions taken

| Decision | Choice |
| --- | --- |
| What is sold | Credit packs (N edits), not a subscription |
| Provider | PayMongo Hosted Checkout v2 (`/v2/checkout_sessions`) |
| Money now | Test/sandbox keys first; live keys are a separate approval |
| Currency | PHP, all amounts in centavos (₱1.00 = `100`) |
| Fulfilment source of truth | `checkout_session.payment.paid` webhook, never the redirect |

Packs live server-side only; the browser never sends a price. Proposed:

| Pack | Price | Edits |
| --- | --- | --- |
| Starter | ₱49 (`4900`) | 10 |
| Plus | ₱99 (`9900`) | 25 |
| Pro | ₱199 (`19900`) | 60 |

PayMongo minimum is typically ₱20, so all three are valid. Adjust prices before
launch; they must stay well above the per-edit model cost (~₱1.5 historical
estimate, not a guarantee).

## Flow

1. User taps a pack in the new "Buy edits" card.
2. Browser `POST /api/checkout` with `{pack, user, email}` (user = the existing
   localStorage token; email = recovery address, required, never used to grant).
3. Server validates the pack and email, records a `pending` payment row, calls
   PayMongo with `line_items`, `payment_method_types`, `reference_number`,
   `customer_email`, `metadata` (`user`, `pack`), `success_url`, `cancel_url`.
4. Browser is redirected to PayMongo's `checkout_url`.
5. PayMongo POSTs `checkout_session.payment.paid` to
   `POST /api/paymongo/webhook`.
6. Server verifies the signature, looks the session up in our `payments` table,
   checks the paid amount, marks it `paid` and adds the pack's edits to
   `users.credits` in one transaction. Duplicate deliveries are no-ops.
7. `success_url` returns to the app with `?paid=1`; the UI polls
   `/api/credits` for ~30 s and shows the new balance. The webhook, not the
   redirect, grants credits — closing the tab after paying still works.

## Data model (`api/credits.py`)

New table:

```sql
payments(
  session_id TEXT PRIMARY KEY,   -- PayMongo cs_...
  user TEXT NOT NULL,
  pack TEXT NOT NULL,
  credits INTEGER NOT NULL,      -- copied from the server-side pack
  amount INTEGER NOT NULL,       -- expected centavos
  status TEXT NOT NULL,          -- pending | paid
  created_at REAL NOT NULL,
  paid_at REAL,
  email TEXT NOT NULL DEFAULT ''  -- buyer-supplied recovery address
)
```

New column: `users.paid_total INTEGER NOT NULL DEFAULT 0` — monotonic count of
purchased edits, used only to recognise "this user has paid before".

Methods: `create_payment(...)`, `mark_paid(session_id, paid_amount)` (atomic:
`BEGIN IMMEDIATE`, `UPDATE ... WHERE status='pending'`, add credits + bump
`paid_total`, return the credited amount or 0 if already paid), `paid_total(user)`.

## Endpoints (`api/server.py`)

| Method | Path | Behaviour |
| --- | --- | --- |
| `GET` | `/api/packs` | `{packs:[{id,label,price,credits}]}` from server-side config |
| `POST` | `/api/checkout` | JSON `{pack, user, email}` → `{checkout_url, session_id}`; `400` unknown pack or bad email; `429` after ~5/hour/IP; `503` when PayMongo keys are unset (no fake success) |
| `POST` | `/api/paymongo/webhook` | raw body; `401` bad/stale signature; always `200` for valid duplicates; `400` unknown session or amount mismatch |

Config from env (`.env.example`, no secrets committed):

```
PAYMONGO_SECRET_KEY=sk_test_...
PAYMONGO_WEBHOOK_SECRET=whsk_...
PAYMENT_SUCCESS_URL=https://freephoto.uft1.com/?paid=1
PAYMENT_CANCEL_URL=https://freephoto.uft1.com/
```

`PAYMENT_SUCCESS_URL`/`CANCEL_URL` must point at the frontend origin, because
the frontend may be the Pages build (`meta[name=api-base]`) rather than the API
host. Webhook registration points at the API origin:
`https://<api-host>/api/paymongo/webhook`.

## Signature verification (must be exactly this)

- Read `await request.body()` before any JSON parsing; the HMAC is over raw bytes.
- Parse `Paymongo-Signature`: `t`, `te`, `li`.
- `HMAC-SHA256(secret, f"{t}.{raw_body}")`, `hmac.compare_digest` against `te`
  (test) or `li` (live) as appropriate.
- Reject when the timestamp is older than 5 minutes (replay window).
- Grant only what our own `payments` row says: pack → credits is server-side.
  Verify the paid amount is at least the recorded `amount`; never trust
  webhook metadata for price or credit count.

## Abuse gates must not punish buyers

Today `MAX_DAILY_EDITS` (60/day global), `IP_DAILY_LIMIT` (24/day/device) and
`FREE_CREDITS` all assume "free". A paying user would hit "today's free budget
is used up". Rule: when `paid_total > 0` and the current balance is > 0, skip
the per-device daily limit and the global daily cap; keep the per-minute IP
limit, the 5-photo validation, and Turnstile. Purchases are revenue-positive,
so relaxing the free-budget gates is intended, not a loophole: the paid balance
is finite and does not regenerate.

`shortcut:` this checks `paid_total`/balance outside the debit transaction, so
a request racing a purchase may still be rejected once. Accepted; tighten only
if it is observed in practice.

Refunds (`store.refund`) keep adding 1 to the same `users.credits` counter. The
ledger has one fungible balance, so no `free`/`paid` bucket split is needed for
correctness.

## UI (`web/`)

- New "Buy edits" card above the donation card: pack buttons with price and
  edit count, a required email field (receipt/recovery, sent as
  `customer_email`), a short "Paid via GCash, Maya, GrabPay or card — handled by
  PayMongo" line, and the existing GCash QR/Ko-fi kept as donations.
- On `?paid=1`, show "Confirming your payment…" and poll `/api/credits` for
  30 s; then "Credits added" or "Still confirming — it can take a minute; the
  credits appear automatically."
- `402` state ("out of credits") links to the buy card instead of dead-ending.
- Update `terms.html` (paid packs, no-refund-once-used policy, support contact)
  and `about.html` (paid packs alongside donations).

## Tests

New `api/test_payments.py`, no network:

- checkout: known pack sends expected centavos/credits/metadata; unknown pack
  `400`; unset keys → `503`; rate limit `429`.
- webhook: valid test signature credits once; duplicate delivery credits once;
  bad signature `401`; stale timestamp `401`; unknown session `400`; amount
  below expected `400`; already-paid session stays a no-op `200`.
- ledger: `mark_paid` is atomic and idempotent; `paid_total` bumps once.
- gates: paid user with balance bypasses daily/global caps; unpaid user still
  gets `429`/`503`; paid user with 0 balance still gets `402`.
- existing suite must stay green: `.venv/bin/python -m pytest api -v`.

## Rollout

1. Implement + tests on a worktree; no commit unless asked.
2. Test mode: follow `docs/payments-sandbox.md` — create PayMongo test keys,
   register the dev webhook (`checkout_session.payment.paid`), run the sandbox
   checkout with a test card or GCash simulator, confirm credits land from the
   webhook even if the success redirect is never loaded.
3. Verify a duplicate webhook and a forged signature against the dev URL with
   `scripts/paymongo_webhook_test.py`.
4. Live mode is a separate approval: live keys, live webhook secret, real ₱20+
   test purchase, then remove the offer if fees/costs do not clear.

## Out of scope (explicit)

- Accounts/login, OAuth, receipt emails, invoices, VAT/BIR handling, refunds
  via API, subscriptions, promo codes, multi-currency.
- Fixing the existing weakness that free credits are minted per self-typed
  user token (no real identity). Payments make a stranded balance possible if
  a buyer loses their localStorage token — support handles it manually at
  first.
- Deploying, DNS/tunnel changes, paid OpenAI calls, or touching the live
  ledger. Those stay separate approved actions.

## Resolved questions

1. Prices: keep ₱49/10 (Starter), ₱99/25 (Plus), ₱199/60 (Pro).
2. Identity: the localStorage token plus a **required email at checkout**, sent
   to PayMongo as `customer_email` and stored on the payments row. The email is
   a manual-recovery path for support, not auth and not a login.
3. Webhook hostname: start with a tunnel to the local API, then the dev API
   (`https://<dev-api-host>/api/paymongo/webhook`); exact dev/prod hostnames
   remain deployment decisions (`deployment-plan.md`). Steps in
   `docs/payments-sandbox.md`.
