# api/

FastAPI backend: upload one photo + an instruction, get an edited photo back.

**Public interface** — `api/server.py` exposes the app (`app`) and these routes:
`POST /api/edit`, `GET /api/result/{id}`, `GET /api/result/{id}/file`,
`GET /api/result/{id}/input/{i}`, `GET /api/jobs`, `GET /api/credits`.
Contract tests: `api/test_edit_contract.py`.

**Allowed dependencies** — strictly downward (`docs/ARCHITECTURE.md`):
`server` → `engines`, `cleanup`, `credits`, `prompts`; `engines` → `prompts`;
`cleanup` → `credits`; `prompts` and `credits` import nothing inside the package.
`test_*.py` may import anything.

**Run tests** — `scripts/check.sh` (never a real ledger or provider key).
**Run locally** — `uvicorn api.server:app --port 8078` from the repo root.

Modules are loaded as package `free_photo_edit_api` by `api/conftest.py`; the
directory name `free-photo-edit` is not a valid Python identifier.
