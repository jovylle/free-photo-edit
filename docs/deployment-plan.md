# Free Photo Edit: reliable deployment plan

Status: proposed, 2026-11-08. This is a plan, not an implemented or approved production migration.

## Goal

Make changes in isolated Git worktrees on the Mac Mini or other machines, test them end to end, merge reviewed changes into `dev`, and deploy the tested commit to a separate development environment on Elitedesk. Promote to `master` and production separately. Keep the frontend available on Cloudflare even when Elitedesk is unavailable, with an honest service-unavailable message.

## Current baseline and constraints

- The development source is `/Volumes/DevSSD/fore/lab/free-photo-edit`; the staged live copy is `~/fore/services/free-photo-edit/app` on the Mac Mini. The live URL is `https://freephoto.uft1.com`. Confirm these facts during the audit rather than assuming they remain current.
- Elitedesk is expected to be reachable from the Mac Mini over SSH as `me@elitedesk`. This is an access assumption, not proof of a successful connection or permission to change the machine. In the audit, verify non-interactive SSH, host identity, least-privilege access and the exact remote paths; never record private keys or credentials.
- The lab checkout and staged live copy contain different uncommitted changes. Preserve and investigate both before creating branches, copying files, or migrating. Do not commit, discard, overwrite, or sync existing work without understanding and approval.
- A separate session records a later live multi-photo deployment with uncommitted work, including a cache-busted frontend asset. Treat that as an audit lead, not a verified current inventory. In particular, do not run `sync-live.sh` over either checkout; its `rsync --delete` can destroy uncommitted work.
- Existing functionality includes credits, quotas, OpenAI image edits, same-origin `/api/*` calls, and photo history. The live service is not assumed to contain the lab's newer changes.
- The lab has a proposed automatic artifact cleanup implementation and associated tests; it has not been deployed. Inspect its scope and obtain approval before any production deletion or rollout.
- The current service uses a consumer-facing bot check. Preserve existing abuse controls unless an explicitly approved replacement supplies equivalent protection; do not introduce new third-party login requirements.
- Deployments, DNS changes, secret transfers, production data migrations, paid image calls, and public exposure are separate actions, not authorized by writing this plan.

## Target environments

| Environment | Code | Location | Data and access |
| --- | --- | --- | --- |
| Local test | Feature branch or worktree | Mac Mini or another developer machine | Temporary SQLite and storage; synthetic fixtures; fake provider; no production access |
| Development | Tested commit merged into `dev` | Cloudflare static frontend; Elitedesk dev backend and worker | Private access; isolated database, files, secrets, budget, and hostname |
| Production | Approved `master` release | Current live service until separately migrated; target is Cloudflare frontend and separate Elitedesk backend | Independent production database, files, secrets, budget, and hostname |

Cloudflare routes the matching frontend's `/api/*` requests to its corresponding backend through a secure, approved connection. Dev and production must never share writable state, worker credentials, or provider secrets. Do not expose the Elitedesk admin or dev service publicly by default. Exact hostnames, tunnel routing, access controls, and remote deployment mechanism are audit decisions.

Cloudflare serving static files can keep the page and an unavailable message visible during an Elitedesk outage; it cannot accept or process edits or serve backend files while the backend is unavailable. Do not advertise this as end-to-end high availability.

## Git and release boundaries

1. Create feature branches and worktrees from a current `dev`; independent machines fetch and push via the Git remote. Never share a mutable worktree between machines.
2. Run the local release checks with isolated fixtures. Push the feature branch and open a PR into `dev`.
3. CI repeats required checks on a clean checkout. Require passing checks and review before merge where repository settings and plan permit. Protect `dev` and `master` from direct writes where supported; document any unavailable protection and its mitigation.
4. Only a successful merge into `dev`, not an open PR or an arbitrary workstation copy, may trigger the single dev deployment authority. Deploy a pinned commit or artifact and record its SHA.
5. Deploy matching frontend and backend versions; verify the pair. A failed or partial rollout must be visible and return to the last compatible version where safe.
6. Promote tested `dev` through a separate PR to `master`. Production deployment is a separate explicit gate. No automatic production cutover is implied.
7. Show environment, frontend/backend version, deployment time, health, worker status, and queue depth in an appropriate private dashboard. Avoid logging image bytes, prompts, credentials, or private identifiers.

## Phased implementation and acceptance gates

### 0. Read-only readiness audit

- [ ] Map both dirty checkouts, current and remote branches, committed revision, PR/CI capabilities, existing deployment and tests. Identify which changes must be preserved and how they relate to production.
- [ ] Verify read-only SSH access from the Mac Mini to `me@elitedesk`, then inspect existing services, capacity, Docker, disk, backups, networking, and deployment tooling without changing them. If SSH or permissions fail, report the blocker; do not change access configuration to work around it. Check current Cloudflare routing and account usage without exposing credentials.
- [ ] Inventory live SQLite, storage, retention, backups, and running jobs using privacy-preserving counts. Establish who can approve migration and deletion. Verify a restorable backup strategy before any cutover.
- [ ] Document findings, proposed branch protection, target hostnames, authentication, costs, and an explicit migration/rollback procedure. Resolve whether a paid Cloudflare plan already exists and what free quota other projects consume.

Gate: user reviews the audit and authorizes implementation or migration separately where needed. No production mutation during audit.

### 1. Test and version-control foundation

- [ ] Reconcile existing uncommitted work safely with its owner; establish `dev`, feature-worktree workflow, and protected `master` without overwriting local changes.
- [ ] Provide one documented local command and a clean CI equivalent for backend and frontend regression tests. Each run uses its own temporary database/storage and explicitly disables inherited live credentials and `.env` behavior.
- [ ] Add deterministic browser-level coverage using synthetic images and a fake provider: upload, validate, queue, process, poll, display and download; cover reload, failure and user-facing availability.
- [ ] Test quotas, credit debit/refund, ownership/access, upload validation, retention/expired files, and no provider calls when rejected. Include regression tests for every behavior changed.
- [ ] Verify clean-checkout execution and concurrent worktrees on supported machines. No test may call production, spend money, or use real user photos by default.

Gate: local and CI tests pass on the same commit, including a demonstrated failing regression when a protected behavior is broken. Document test limitations; mocks do not prove actual AI quality or live service availability.

### 2. Durable jobs and recovery

- [ ] Define job states and transitions including queued, processing, completed, failed and needs-attention. Keep one authoritative job record and one credit ledger.
- [ ] Atomically claim jobs; record worker ownership/lease and recover expired claims. Test concurrent claims, worker termination, restart, stale responses, idempotent completion, and reconciliation.
- [ ] Keep the global rolling spend cap, per-user limits, validation and abuse gates effective across API and worker processes. Reserve/debit atomically before processing; rejected requests must not invoke a provider. Avoid relying on in-process semaphores for cross-process limits.
- [ ] Distinguish confirmed provider failure from an ambiguous timeout after submission. Avoid automatically retrying a potentially billable request unless provider idempotency is verified; surface uncertainty for review. Test refund and duplicate-submission behavior.
- [ ] Keep the existing OpenAI integration as the first supported processor. Future providers and manual workflows must respect their own terms and privacy policies; no ChatGPT consumer-site automation is assumed.

Gate: deterministic integration and browser tests demonstrate complete flow and failure recovery, including no duplicate ledger effects. Paid provider behavior remains a separate test.

### 3. Isolated Elitedesk dev deployment

- [ ] Provision separate dev app and worker using the audited deployment mechanism. Pin the deployed revision; persist dev SQLite and files outside disposable containers; configure health checks, restart behavior and least-privilege secrets.
- [ ] Deploy a private Cloudflare dev frontend and connect only to the dev API. Verify `/api/*` routing, access boundaries, unavailable state, and safe upload/download behavior.
- [ ] Automate dev deployment only after a passing PR merges to `dev`. Prevent overlapping deployments and report failures and the running SHA for each component.
- [ ] Add monitored worker heartbeat, stuck jobs, failures, disk capacity, and a tested backup/restore. Configure the agreed small dev spending cap.
- [ ] Run end-to-end against the deployed dev stack with a fake provider first; then, only with spending authorization and synthetic data, run a bounded real-provider smoke test.

Gate: clean deployment and rollback exercise; full deployed dev flow, outage recovery, alerts and restore demonstrated. Dev and production isolation independently verified.

### 4. Production readiness and migration

- [ ] Verify retention behavior and existing photo inventory. Agree on retention and deletion policy; test cleanup locally and in dev. Obtain explicit approval before deleting live files.
- [ ] Rehearse migration using an isolated backup copy. Verify SQLite consistency, job and credit totals, required photos, permissions and checksums without exposing private data. Define handling for running jobs and rollback/data reconciliation.
- [ ] Set up separate production frontend, backend, worker, storage and secrets. Preserve the public domain and compatibility with existing users, subject to verified routing and privacy controls.
- [ ] Agree on a maintenance/cutover window, monitoring, backup, rollback triggers, and who approves the change. Account for new writes after cutover; reverting code or DNS alone does not restore database state.
- [ ] After explicit production authorization, deploy the pinned release, verify health and a bounded real edit, monitor, and retain recovery artifacts according to the approved policy.

Gate: verified end-to-end production functionality, preserved data, accurate deployed versions, documented rollback and monitored worker. No Facebook publication is part of this plan.

## Capacity and costs

- Start with one worker and bounded concurrency. Measure wait time and provider latency before increasing capacity.
- Set explicit per-user, global daily and provider spending limits, plus upload size and retention limits. Present a clear wait or capacity message instead of accepting unbounded work.
- Cloudflare static hosting does not pay for image-model calls. Free-tier quotas and account usage are shared with other projects; measure requests including status polling before promising headroom. If free limits are exhausted, the service may fail until reset; paid plans can incur charges. Do not enable paid services without approval.
- Monitor infrastructure usage and actual provider charges separately. The previously quoted per-edit figure is only an estimate, not a verified bill.

## Open decisions

1. Which existing dirty changes should enter the first feature branch, and how should the staged/live differences be preserved?
2. Which private dev hostname and secure access mechanism should be used on Elitedesk?
3. Which CI and deployment mechanism has the necessary permissions and can serialize deployments of matching frontend/backend artifacts?
4. What are the approved dev and production budgets, retention period, recovery objectives, and availability expectations?
5. Who approves the production migration, any live cleanup, and a real paid edit?

## Not in scope

Automating the consumer ChatGPT website, bypassing limits, posting on Facebook, replacing the image provider without evaluation, promising zero downtime, or moving production data as part of this planning document.
