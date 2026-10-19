"""free-photo-edit backend (v1).

POST /api/edit   multipart: image (x1), prompt, preset, user
  -> 202 {job_id, status: running} immediately; poll GET /api/result/{id}
GET  /api/result/{job_id}
GET  /api/result/{job_id}/file  (output PNG when done)
GET  /api/result/{job_id}/input/0  (stored input thumb)
GET  /api/jobs?user=  (last 30 jobs, newest-first)
GET  /api/credits
GET  /api/presets
GET  /api/config  (turnstile sitekey for the widget, or null in dev)

Adapted from photo-fusion/api/server.py (1-photo v1: single image field,
no admin layer, no retry route; engine interface is edit(bytes, prompt).
Ruling 2026-10-18 (public launch): abuse gates run BEFORE slots/debit so
rejections cost zero — Turnstile (fail closed when its secret is set),
per-IP rolling minute window and per-device (IP+UA hash) rolling-24h
window, global rolling-24h spend cap. The old
lifetime IP_FREE_LIMIT=9 is retired: behind shared NAT a lifetime cap
punishes legit households, and a self-typed user id was never identity.
"""

from __future__ import annotations

import os
import hashlib
import threading
import time
import traceback
import uuid
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import httpx

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from .credits import FREE_CREDITS, CreditsStore, OutOfCredits
from .engines import EngineError, select_engine
from .prompts import PRESETS, build_edit_prompt, resolve_preset

API_DIR = Path(__file__).resolve().parent
STORAGE = Path(os.environ.get("FREE_PHOTO_EDIT_STORAGE", API_DIR / "storage"))
DB_PATH = os.environ.get("FREE_PHOTO_EDIT_DB", str(API_DIR / "credits.db"))

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_PROMPT_CHARS = 2000
ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp"}
ALLOWED_PRESETS = {"background", "clothes", "restore", "custom"}

FREE_NOTE = "3 free edits per user. Outputs up to 1024x1024."

app = FastAPI(title="free-photo-edit api (v1)")

_store: CreditsStore | None = None
_store_lock = threading.Lock()

IP_MINUTE_LIMIT = int(os.environ.get("IP_MINUTE_LIMIT", "3"))
# Per IP+UA (device) so friends behind one home WiFi each get their own
# daily bucket. UA is spoofable — fine for a generosity cap, not security.
IP_DAILY_LIMIT = int(os.environ.get("IP_DAILY_LIMIT", "24"))
MAX_DAILY_EDITS = int(os.environ.get("MAX_DAILY_EDITS", "60"))

TURNSTILE_SITEKEY = os.environ.get("TURNSTILE_SITEKEY", "")
TURNSTILE_SECRET = os.environ.get("TURNSTILE_SECRET", "")
TURNSTILE_HOSTNAMES = {
    h.strip().lower() for h in os.environ.get("TURNSTILE_HOSTNAMES", "").split(",")
    if h.strip()
}
TURNSTILE_ACTION = "edit"
TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

_warned_no_turnstile = False


class TurnstileError(RuntimeError):
    pass


def _verify_turnstile(token: str, ip: str) -> None:
    """Canonical siteverify: success + exact action + approved hostname.

    Raises TurnstileError on any failure (fail closed). Tokens are
    single-use: Cloudflare rejects replays at siteverify.
    """
    if not token or len(token) > 2048 or not TURNSTILE_HOSTNAMES:
        raise TurnstileError("missing token or hostname allowlist")
    try:
        r = httpx.post(TURNSTILE_VERIFY_URL,
                       data={"secret": TURNSTILE_SECRET,
                             "response": token, "remoteip": ip},
                       timeout=10)
    except Exception as e:
        raise TurnstileError(
            f"siteverify unreachable: {type(e).__name__}") from e
    if r.status_code != 200:
        raise TurnstileError(f"siteverify HTTP {r.status_code}")
    try:
        result = r.json()
    except Exception:
        raise TurnstileError("siteverify bad response") from None
    if (not result.get("success")
            or result.get("action") != TURNSTILE_ACTION
            or str(result.get("hostname", "")).lower()
            not in TURNSTILE_HOSTNAMES):
        raise TurnstileError("token rejected")


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("CF-Connecting-IP", "")
    if fwd.strip():
        return fwd.strip()[:64]
    xff = request.headers.get("X-Forwarded-For", "")
    if xff.strip():
        return xff.split(",")[0].strip()[:64]
    try:
        return (request.client.host if request.client else "?")[:64]
    except Exception:
        return "?"


def _client_device(request: Request, ip: str) -> str:
    """Stable per-device key: sha(ip + User-Agent)[:32].

    UA is spoofable — generous-cap key only, not auth.
    """
    raw = request.headers.get("User-Agent", "").strip()
    if not raw:
        return ""
    return hashlib.sha256(f"{ip}|{raw}".encode()).hexdigest()[:32]


MAX_PER_USER = 3
MAX_GLOBAL = 6
_user_slots: dict[str, threading.Semaphore] = {}
_user_slots_guard = threading.Lock()
_global_slots = threading.Semaphore(MAX_GLOBAL)


def _slots_for(user: str) -> threading.Semaphore:
    with _user_slots_guard:
        sem = _user_slots.get(user)
        if sem is None:
            sem = threading.Semaphore(MAX_PER_USER)
            _user_slots[user] = sem
        return sem


def reset_slots_for_tests() -> None:
    """Test hook: drop in-memory slot state between tests."""
    global _global_slots
    with _user_slots_guard:
        _user_slots.clear()
    _global_slots = threading.Semaphore(MAX_GLOBAL)


def get_store() -> CreditsStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                STORAGE.mkdir(parents=True, exist_ok=True)
                _store = CreditsStore(DB_PATH)
    return _store


def reset_store_for_tests(path: str) -> CreditsStore:
    """Test hook: swap the singleton store for a tmp-path DB."""
    global _store
    with _store_lock:
        _store = CreditsStore(path)
        return _store


def current_user(
    x_user: str | None = Header(default=None),
    user: str | None = Query(default=None),
) -> str:
    return ((x_user or user) or "dev").strip() or "dev"


@app.get("/api/credits")
def credits(user: str = Depends(current_user)):
    return {"user": user, "credits_left": get_store().balance(user)}


@app.get("/api/config")
def config():
    return {"turnstile_sitekey": TURNSTILE_SITEKEY or None}


@app.get("/api/presets")
def presets():
    return {"presets": [
        {"id": "background", "label": "🏙 Background",
         "hint": "New background from your description. 1 photo."},
        {"id": "clothes", "label": "👔 Clothes",
         "hint": "Change clothing from your description. 1 photo."},
        {"id": "restore", "label": "✨ Restore",
         "hint": "Fix scratches, noise, blur, faded colors. 1 photo."},
        {"id": "custom", "label": "✏ Custom",
         "hint": "Free-form instruction (Tagalog/English/Bisaya)."},
    ]}


@app.get("/api/result/{job_id}")
def result(job_id: str, user: str = Depends(current_user)):
    store = get_store()
    job = store.get_job(job_id)
    if job is None:
        raise HTTPException(404, "unknown job")
    base = {"job_id": job_id, "credits_left": store.balance(user),
            "preset": job["preset"], "created_at": job["created_at"]}
    if job["status"] != "done":
        return {**base, "status": job["status"], "image_url": None}
    png = STORAGE / job_id / "output.png"
    if not png.exists():
        raise HTTPException(500, "output missing")
    return {**base, "status": "done",
            "image_url": f"/api/result/{job_id}/file"}


_INPUT_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".png": "image/png", ".webp": "image/webp"}


@app.get("/api/result/{job_id}/input/0")
def result_input(job_id: str):
    store = get_store()
    job = store.get_job(job_id)
    if job is None:
        raise HTTPException(404, "unknown job")
    job_dir = STORAGE / job_id
    for ext, mime in _INPUT_MIME.items():
        p = job_dir / f"input_0{ext}"
        if p.exists():
            return FileResponse(p, media_type=mime)
    raise HTTPException(404, "input not found")


@app.get("/api/jobs")
def jobs(user: str = Depends(current_user)):
    store = get_store()
    items = []
    for j in store.user_jobs(user, limit=30):
        job_id = j["id"]
        image_url = (f"/api/result/{job_id}/file"
                     if j["status"] == "done" else None)
        inputs = [f"/api/result/{job_id}/input/0"]
        items.append({**j, "job_id": job_id, "inputs": inputs,
                      "image_url": image_url})
    return {"user": user, "fetched_at": time.time(), "jobs": items}


@app.get("/api/result/{job_id}/file")
def result_file(job_id: str):
    store = get_store()
    job = store.get_job(job_id)
    if job is None:
        raise HTTPException(404, "unknown job")
    png = STORAGE / job_id / "output.png"
    if job["status"] != "done" or not png.exists():
        raise HTTPException(404, "result not ready")
    return FileResponse(png, media_type="image/png",
                        filename=f"free-photo-edit-{job_id}.png")


@app.post("/api/edit", status_code=202)
async def edit(
    request: Request,
    prompt: str = Form(""),
    preset: str = Form("custom"),
    user: str = Form("dev"),
    cf_token: str = Form(default="", alias="cf-turnstile-response"),
    image: UploadFile = File(...),
):
    user = (user or "dev").strip() or "dev"
    preset = (preset or "custom").strip().lower()
    ip = _client_ip(request)
    device = _client_device(request, ip)
    store = get_store()
    global _warned_no_turnstile
    if TURNSTILE_SECRET:
        try:
            _verify_turnstile(cf_token, ip)
        except TurnstileError as e:
            raise HTTPException(403, f"bot check failed: {e}")
    elif not _warned_no_turnstile:
        _warned_no_turnstile = True
        print("WARN: TURNSTILE_SECRET unset — bot check OFF (dev only)")
    now = time.time()
    if store.count_jobs_since(now - 86400) >= MAX_DAILY_EDITS:
        raise HTTPException(
            503, "today's free budget is used up — try again tomorrow",
            headers={"Retry-After": "3600"})
    if store.ip_job_count_since(ip, now - 60) >= IP_MINUTE_LIMIT:
        raise HTTPException(
            429, "too many tries — wait a minute and try again",
            headers={"Retry-After": "60"})
    if store.ip_job_count_since(ip, now - 86400, ua=device) >= IP_DAILY_LIMIT:
        raise HTTPException(
            429, f"this device has used its {IP_DAILY_LIMIT} free runs today",
            headers={"Retry-After": "3600"})
    if len(prompt) > MAX_PROMPT_CHARS:
        raise HTTPException(
            400, f"prompt too long: max {MAX_PROMPT_CHARS} chars "
                 f"(got {len(prompt)})")
    if preset not in ALLOWED_PRESETS or preset not in PRESETS:
        raise HTTPException(400, f"unknown preset '{preset}'")
    try:
        instruction = resolve_preset(preset, prompt)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not instruction:
        raise HTTPException(400, "prompt must not be empty")
    if image.content_type not in ALLOWED_MIME:
        raise HTTPException(
            400, f"unsupported type {image.content_type}: jpeg/png/webp only")
    data = await image.read()
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(400, f"file too large: max 5MB "
                                 f"(got {len(data)} bytes)")
    if not data:
        raise HTTPException(400, "empty file rejected")

    user_sem = _slots_for(user)
    if not user_sem.acquire(blocking=False):
        raise HTTPException(
            429, f"{MAX_PER_USER} concurrent jobs per user — try again shortly",
            headers={"Retry-After": "10"})
    if not _global_slots.acquire(blocking=False):
        user_sem.release()
        raise HTTPException(
            429, "server is busy (all job slots full) — try again shortly",
            headers={"Retry-After": "15"})
    try:
        job_id = uuid.uuid4().hex[:12]
        try:
            credits_left = store.check_and_debit(
                user, job_id, preset=preset, ip=ip, ua=device, prompt=prompt)
        except OutOfCredits:
            user_sem.release()
            _global_slots.release()
            raise HTTPException(402, "out of credits")

        job_dir = STORAGE / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        ext = {"image/jpeg": ".jpg", "image/png": ".png",
               "image/webp": ".webp"}
        (job_dir / f"input_0{ext[image.content_type]}").write_bytes(data)

        t = threading.Thread(
            target=_run_job_thread,
            args=(job_id, user, data, instruction, user),
            daemon=True)
        t.start()
    except HTTPException:
        raise
    except Exception:
        user_sem.release()
        _global_slots.release()
        raise
    return {"job_id": job_id, "status": "running", "credits_left": credits_left,
            "note": FREE_NOTE}


def _run_job_thread(job_id: str, user: str, input_bytes: bytes,
                    instruction: str, slot_key: str) -> None:
    """Background worker (daemon thread): engine -> done | failed+refund."""
    store = get_store()
    try:
        try:
            full_prompt = build_edit_prompt(instruction)
            engine = select_engine()
            out_bytes = engine.edit(input_bytes, full_prompt)
            job_dir = STORAGE / job_id
            job_dir.mkdir(parents=True, exist_ok=True)
            (job_dir / "output.png").write_bytes(out_bytes)
        except Exception as e:
            traceback.print_exc()
            err = f"{type(e).__name__}: {e}"[:500]
            job = store.get_job(job_id)
            if job is not None:
                store.refund(user, job_id, err)
            else:
                store.fail(job_id, err)
            return
        store.complete(job_id, "done")
    finally:
        _global_slots.release()
        _slots_for(slot_key).release()


# Static UI must mount AFTER all /api/* routes (Starlette matches in order).
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
