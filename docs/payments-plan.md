# Free Photo Edit: basic paid credit packs

Status: implemented on worktree 2026-11-11 (tests green, 111 passed); sandbox
runbook ready in `docs/payments-sandbox.md`. Sandbox and live rollout not done;
nothing charged.

Provider changed from PayMongo to **Xendit** on 2026-11-11: PayMongo had paused
new sign-ups, Xendit issues test keys immediately.

## Goal

Let a visitor buy a pack of edits with GCash, Maya, GrabPay or card, and have
those edits land in the existing credits ledger. Keep the current 3 free edits
as a trial. Payments are handled by Xendit's hosted checkout; the app never sees
card data.

## Decisions taken

| Decision | Choice |
| --- | --- |
| What is sold | Credit packs (N edits), not a subscription |
| Provider | Xendit Payment Sessions, `mode: PAYMENT_LINK` (`POST /sessions`) |
| Money now | Test keys first; live keys are a separate approval after KYC |
| Currency | PHP; packs priced in centavos, sent to Xendit in whole pesos |
| Fulfilment source of truth | `payment_session.completed` webhook, never the redirect |

Packs live server-side only; the browser never sends a price:

| Pack | Price | Edits |
| --- | --- | --- |
| Starter | ₱49 (`4900`) | 10 |
| Plus | ₱99 (`9900`) | 25 |
| Pro | ₱199 (`19900`) | 60 |

Prices must stay well above the per-edit model cost (~₱1.5 historical estimate,
not a guarantee) plus the channel fee. Re-check Xendit's current PH rate before
launch; a fixed/minimum fee can eat a ₱49 pack.

## Flow

1. User taps a pack in the "Buy edits" card.
2. Browser `POST /api/checkout` with `{pack, user, email}` (user = the existing
   localStorage token; email = recovery address, required, never used to grant).
3. Server validates the pack and email, calls Xendit with `reference_id`,
   `session_type: PAY`, `mode: PAYMENT_LINK`, `amount`, `currency: PHP`,
   `country: PH`, `items`, `success_return_url`, `cancel_return_url`,
   `metadata` (`user`, `pack`, `email`), then records a `pending` row.
4. Browser is redirected to Xendit's `payment_link_url`.
5. Xendit POSTs `payment_session.completed` to `POST /api/xendit/webhook`.
6. Server checks the `x-callback-token` header, looks the session up in our
   `payments` table, checks the paid amount, marks it `paid` and adds the pack's
   edits to `users.credits` in one transaction. Duplicate deliveries are no-ops.
7. `success_return_url` returns to the app with `?paid=1`; the UI polls
   `/api/credits` for ~30 s and shows the new balance. The webhook, not the
   redirect, grants credits — closing the tab after paying still works.

## Data model (`api/credits.py`)

New table:

```sql
payments(
  session_id TEXT PRIMARY KEY,   -- Xendit ps_...
  user TEXT NOT NULL,
  pack TEXT NOT NULL,
  credits INTEGER NOT NULL,      -- copied from the server-side pack
  amount INTEGER NOT NULL,       -- amount we sent to Xendit (whole pesos)
  status TEXT NOT NULL,          -- pending | paid
  created_at REAL NOT NULL,
  paid_at REAL,
  email TEXT NOT NULL DEFAULT ''  -- buyer-supplied recovery address
)
```

`amount` stores exactly what was sent to the provider, so the webhook compares
like-for-like and no unit conversion happens twice.

New column: `users.paid_total INTEGER NOT NULL DEFAULT 0` — monotonic count of
purchased edits, used only to recognise "this user has paid before".

Methods: `create_payment(...)`, `mark_paid(session_id, paid_amount)` (atomic:
`BEGIN IMMEDIATE`, `UPDATE ... WHERE status='pending'`, add credits + bump
`paid_total`, return the credited amount or 0 if already paid), `paid_total(user)`.

## Endpoints (`api/server.py`)

| Method | Path | Behaviour |
| --- | --- | --- |
| `GET` | `/api/packs` | `{packs:[{id,label,price,credits}]}` from server-side config; empty when unconfigured |
| `POST` | `/api/checkout` | JSON `{pack, user, email}` → `{checkout_url, session_id}`; `400` unknown pack or bad email; `429` after ~5/hour/IP; `503` when Xendit keys are unset (no fake success) |
| `POST` | `/api/xendit/webhook` | `x-callback-token` header; `401` bad token; `200` for duplicates and unknown sessions; `400` amount mismatch |

Config from env (`.env.example`, no secrets committed):

```
XENDIT_SECRET_KEY=xnd_development_...
XENDIT_WEBHOOK_TOKEN=...
PAYMENT_SUCCESS_URL=https://freephoto.uft1.com/?paid=1
PAYMENT_CANCEL_URL=https://freephoto.uft1.com/
```

`PAYMENT_SUCCESS_URL`/`CANCEL_URL` must point at the frontend origin, because
the frontend may be the Pages build (`meta[name=api-base]`) rather than the API
host. Xendit requires **HTTPS** return URLs. Webhook registration points at the
API origin: `https://<api-host>/api/xendit/webhook`.

## Amount unit (verified 2026-11-11)

Xendit's docs are inconsistent about whether `amount` is whole pesos or
centavos. `_xendit_amount()` sends whole pesos (`4900 → 49`) and the ledger
stores what was sent.

Verified against the test key: a session created with `amount: 49` renders
**₱49** on the hosted page. Re-check this whenever a pack price changes; the
safe failure mode is a ₱0.49 charge, never ₱4,900.

## Webhook verification

- Static token: compare `x-callback-token` with `XENDIT_WEBHOOK_TOKEN` using
  `hmac.compare_digest`. No timestamp, so no replay window — the duplicate
  no-op is what makes a replayed delivery harmless.
- Grant only what our own `payments` row says: pack → credits is server-side.
  Verify the paid amount is at least the recorded `amount`; never trust
  webhook metadata for price or credit count.
- `payment_session.expired` and any other event is a `200` no-op.

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
  edit count, a required email field (receipt/recovery, also sent to Xendit in
  `metadata`), a short "Paid via GCash, Maya, GrabPay or card — handled by
  Xendit" line, and the existing GCash QR/Ko-fi kept as donations.
- On `?paid=1`, show "Confirming your payment…" and poll `/api/credits` for
  30 s; then "Credits added" or "Still confirming — it can take a minute; the
  credits appear automatically."
- `402` state ("out of credits") links to the buy card instead of dead-ending.
- Update `terms.html` (paid packs, no-refund-once-used policy, support contact)
  and `about.html` (paid packs alongside donations).

## Tests

`api/test_payments.py`, no network:

- checkout: known pack sends the expected amount/items/metadata; unknown pack
  `400`; bad email `400`; unset keys → `503`; rate limit `429`.
- webhook: valid token credits once; duplicate delivery credits once; bad or
  missing token `401`; unknown session `200` no-op; amount below expected `400`;
  other events are a `200` no-op; unset token `503`.
- ledger: `mark_paid` is atomic and idempotent; `paid_total` bumps once.
- gates: paid user with balance bypasses daily/global caps; unpaid user still
  gets `429`/`503`; paid user with 0 balance still gets `402`.
- existing suite must stay green: `.venv/bin/python -m pytest api -v`.

## Rollout

1. Implement + tests on a worktree.
2. Test mode: follow `docs/payments-sandbox.md` — test key + webhook token,
   register the dev webhook (`payment_session.completed`), run the sandbox
   checkout, confirm credits land from the webhook even if the redirect is
   never loaded.
3. Verify duplicate, forged-token, low-amount and unknown-session deliveries
   against the dev URL with `scripts/xendit_webhook_test.py`.
4. Live mode is a separate approval: business KYC, live key, live webhook
   token, one real purchase, then remove the offer if fees/costs do not clear.

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
2. Identity: the localStorage token plus a **required email at checkout**, kept
   on the payments row and sent to Xendit in `metadata`. The email is a
   manual-recovery path for support, not auth and not a login.
3. Webhook hostname: start with a tunnel to the local API, then the dev API
   (`https://<dev-api-host>/api/xendit/webhook`); exact dev/prod hostnames
   remain deployment decisions (`deployment-plan.md`). Steps in
   `docs/payments-sandbox.md`.
