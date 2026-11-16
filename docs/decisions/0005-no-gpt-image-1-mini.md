# 0005 — No gpt-image-1-mini, at any price

**Status** — accepted (2026-11-16)

**Context** — `gpt-image-2.5-sunburst` costs ~2.64¢/edit (₱1.69) and
`gpt-image-1-mini` costs ~1.3¢ (₱0.83) — exactly half. The 2026-10-17 probes had
already flagged mini for repainting faces and garbling ID and phone text, and
sunburst replaced it as default. By 2026-11-16 the free tier was the only
remaining cost argument, so the question was re-opened and re-tested rather than
settled by memory.

**Decision** — Mini is not an option. One real portrait was pushed through both
models with the app's own `background` preset ("a clean soft-grey studio
backdrop") and the identical prompt scaffold. Mini runs fine — 1024px, 13.8s,
half the price, output as large as sunburst's — but it **redraws the face**:
freckles, the moles above the lip and on the cheek, and the hair edges against
the new backdrop all change. Sunburst keeps them.

**Why the halved cost is refused** — The product's entire promise is "keep the
subject exactly the same, change only what was asked" (see `INVARIANTS` in
`api/prompts.py`). A face that is nearly the same person is a failed edit, not a
cheap one, and the refund/recovery path cannot detect it — the job succeeds and
the user finds out by looking. At the current volume the saving is ~₱250/month
total, which does not buy back a single lost user.

**Consequences** — Easy: edit cost is predictable and identity holds, so the
60/day ceiling stays a hard ₱101/day bound. Impossible: selecting mini by
environment — `OPENAI_MODEL` stays a module constant, deliberately without an
`os.environ` override, so a misconfigured `.env` cannot quietly swap the model
under production. Accepted cost: twice the per-edit price, and a re-test is
needed before any future cheaper model is adopted. Revisit only with the same
protocol — a real portrait, the app's own preset, and a look at the face, not at
the invoice. Recorded in the `api/engines.py` history so the next reader sees
why before they see the price.
