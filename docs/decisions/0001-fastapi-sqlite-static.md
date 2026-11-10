# 0001 — FastAPI + SQLite + static UI, one container

**Status**: accepted (2026-10-11, recorded here 2026-11-11)

## Context

The product must run at zero paid burn: 3 free edits per IP per day, no login,
no card. That rules out managed databases with per-seat costs and any runtime
that bills per request. A Cloudflare Worker was considered and rejected as the
API runtime: the free Workers AI quota is accessed over its HTTP API anyway, and
FastAPI gives us multipart handling, background threads and tests for free.

## Decision

- Runtime: FastAPI app in `api/`, static UI in `web/`, served from one container.
- Ledger: a single SQLite file (`api/credits.db`), tables `users` + `jobs`,
  check-then-debit inside `BEGIN IMMEDIATE`, refund on engine failure.
- Jobs: `POST /api/edit` returns `202 {job_id}` and a background thread does the
  work; the browser polls `/api/result/{id}`. No queue infrastructure.
- Providers: free tiers only, behind one engine interface. Keys stay server-side.
- Storage: local files under `api/storage/<job_id>/`, swept on a short TTL.

## Consequences

- Easy: one deployable, transactional quota, tests run offline with a fake engine.
- Impossible: horizontal scaling of the API without moving the ledger and storage.
  Accepted — the free quota caps load well under one process's capacity.
- Accepted: SQLite + local files means a single-writer deployment; backup is a
  file copy. Revisit only if daily volume exceeds one box.
- The split dev topology (static UI on Pages, API on its own host) is allowed by
  `meta[name=api-base]` + `CORS_ORIGINS`, without changing this decision.
