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
import asyncio
from contextlib import asynccontextmanager
import hmac
import json
import logging
import hashlib
import re
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import TypedDict

from dotenv import load_dotenv
from fastapi import Body, Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import UploadFile as StarletteUploadFile

import httpx

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from .credits import DailyLimitReached, FREE_CREDITS, CreditsStore, OutOfCredits
from .cleanup import artifacts_expired, sweep, SWEEP_INTERVAL_SECONDS
from .engines import EngineError, select_engine
from .prompts import PRESETS, build_edit_prompt, resolve_preset

API_DIR = Path(__file__).resolve().parent
STORAGE = Path(os.environ.get("FREE_PHOTO_EDIT_STORAGE", API_DIR / "storage"))
DB_PATH = os.environ.get("FREE_PHOTO_EDIT_DB", str(API_DIR / "credits.db"))

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_IMAGES = 5
MAX_PROMPT_CHARS = 2000
ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp"}
ALLOWED_PRESETS = {"background", "clothes", "restore", "custom"}

FREE_NOTE = "5 free edits a day per user. Outputs up to 1024x1024."
STARTED_AT = time.time()

@asynccontextmanager
async def lifespan(app):
    store = get_store()
    storage = STORAGE
    stop = threading.Event()

    def run_sweep():
        try:
            sweep(store, storage)
        except Exception:
            logging.getLogger(__name__).exception("TTL sweep failed; retry next hour")

    def periodic():
        while not stop.wait(SWEEP_INTERVAL_SECONDS):
            run_sweep()

    await asyncio.to_thread(run_sweep)
    worker = threading.Thread(target=periodic, name="fpe-storage-ttl", daemon=True)
    worker.start()
    try:
        yield
    finally:
        stop.set()
        await asyncio.to_thread(worker.join)


app = FastAPI(title="free-photo-edit api (v1)", lifespan=lifespan)

_store: CreditsStore | None = None
_store_lock = threading.Lock()

IP_MINUTE_LIMIT = int(os.environ.get("IP_MINUTE_LIMIT", "3"))
# Per-IP daily ceiling. This is the one a VPN-less abuser cannot dodge by
# opening another browser or switching User-Agent on the same network.
IP_DAILY_LIMIT = int(os.environ.get("IP_DAILY_LIMIT", "24"))
# Per IP+UA (device) so friends behind one home WiFi each get their own
# daily bucket and one browser cannot eat the household's whole day.
# UA is spoofable — fine for a generosity cap, not security.
DEVICE_DAILY_LIMIT = int(os.environ.get("DEVICE_DAILY_LIMIT", "12"))
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

# --- Paid edit packs (server-side prices; the browser never sends one) ---
# Amounts are centavos (PHP): ₱49.00 = 4900.
class Pack(TypedDict):
    label: str
    price: int
    credits: int


PACKS: dict[str, Pack] = {
    "starter": {"label": "Starter", "price": 4900, "credits": 10},
    "plus": {"label": "Plus", "price": 9900, "credits": 25},
    "pro": {"label": "Pro", "price": 19900, "credits": 60},
}
XENDIT_API = "https://api.xendit.co/sessions"
XENDIT_SECRET_KEY = os.environ.get("XENDIT_SECRET_KEY", "")
XENDIT_WEBHOOK_TOKEN = os.environ.get("XENDIT_WEBHOOK_TOKEN", "")
PAYMENT_SUCCESS_URL = os.environ.get("PAYMENT_SUCCESS_URL", "")
PAYMENT_CANCEL_URL = os.environ.get("PAYMENT_CANCEL_URL", "")
CHECKOUT_PER_HOUR = int(os.environ.get("CHECKOUT_PER_HOUR", "5"))
EMAIL_RE = re.compile(r"^[^@\s]{1,100}@[^@\s]{1,100}\.[^@\s]{1,100}$")

_checkout_hits: dict[str, list[float]] = {}
_checkout_guard = threading.Lock()


def _checkout_allowed(ip: str, now: float) -> bool:
    with _checkout_guard:
        hits = [t for t in _checkout_hits.get(ip, []) if t > now - 3600]
        _checkout_hits[ip] = hits
        if len(hits) >= CHECKOUT_PER_HOUR:
            return False
        hits.append(now)
        return True


class XenditError(RuntimeError):
    pass


def _xendit_amount(centavos: int) -> int:
    """Xendit takes whole pesos. The ledger stores what we send, so the
    webhook compares like-for-like. Confirm the unit on the first sandbox
    session: the hosted page must read ₱49.00, never ₱4,900 or ₱0.49.
    """
    return centavos // 100


def _checkout_payload(pack_id: str, user: str, reference: str,
                      email: str) -> dict[str, object]:
    pack = PACKS[pack_id]
    amount = _xendit_amount(pack["price"])
    return {
        "reference_id": reference,
        "session_type": "PAY",
        "mode": "PAYMENT_LINK",
        "amount": amount,
        "currency": "PHP",
        "country": "PH",
        "description": f"Free Photo Edit — {pack['label']}: {pack['credits']} edits",
        "items": [{"reference_id": pack_id, "type": "DIGITAL_SERVICE",
                   "name": f"{pack['label']} — {pack['credits']} edits",
                   "net_unit_amount": amount, "quantity": 1,
                   "category": "Photo editing"}],
        "success_return_url": PAYMENT_SUCCESS_URL,
        "cancel_return_url": PAYMENT_CANCEL_URL,
        "metadata": {"user": user, "pack": pack_id, "email": email},
    }


def _create_checkout_session(pack_id: str, user: str,
                             email: str) -> tuple[str, str]:
    """Create a Xendit payment session -> (session_id, checkout_url)."""
    r = httpx.post(XENDIT_API, auth=(XENDIT_SECRET_KEY, ""),
                   json=_checkout_payload(pack_id, user, uuid.uuid4().hex,
                                          email),
                   timeout=20)
    if r.status_code not in (200, 201):
        raise XenditError(f"Xendit HTTP {r.status_code}")
    try:
        data = r.json()
        return data["payment_session_id"], data["payment_link_url"]
    except (KeyError, TypeError):
        raise XenditError("Xendit bad response") from None


def _verify_webhook_token(token: str) -> None:
    """Xendit sends a static token in the x-callback-token header."""
    if not XENDIT_WEBHOOK_TOKEN:
        raise HTTPException(503, "payments not configured")
    if not token or not hmac.compare_digest(token, XENDIT_WEBHOOK_TOKEN):
        raise HTTPException(401, "bad token")


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


MAX_PER_IP = 3
MAX_GLOBAL = 6
_ip_slots: dict[str, threading.Semaphore] = {}
_ip_slots_guard = threading.Lock()
_global_slots = threading.Semaphore(MAX_GLOBAL)


def _slots_for(ip: str) -> threading.Semaphore:
    with _ip_slots_guard:
        sem = _ip_slots.get(ip)
        if sem is None:
            sem = threading.Semaphore(MAX_PER_IP)
            _ip_slots[ip] = sem
        return sem


def reset_slots_for_tests() -> None:
    """Test hook: drop in-memory slot state between tests."""
    global _global_slots
    with _ip_slots_guard:
        _ip_slots.clear()
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
def config() -> dict[str, str | None]:
    return {"turnstile_sitekey": TURNSTILE_SITEKEY or None,
            "revision": _revision()}


_REVISION_CACHE: str | None = None


def _revision() -> str:
    """Revision stamped by `homeploy push` after rsync; 'dev' in a local run."""
    global _REVISION_CACHE
    if _REVISION_CACHE is not None:
        return _REVISION_CACHE
    env = os.environ.get("FREE_PHOTO_EDIT_REVISION_FILE")
    path = Path(env) if env else (API_DIR.parent / "REVISION")
    try:
        _REVISION_CACHE = path.read_text().strip() or "unknown"
    except OSError:
        _REVISION_CACHE = "unknown"
    return _REVISION_CACHE


def _prom_label(v: object) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"')


@app.get("/metrics")
def metrics() -> Response:
    """Prometheus text format: aggregate counts only — no users, prompts or images."""
    s = get_store().stats()
    lines = [
        "# HELP photoedit_up 1 while the API is serving.",
        "# TYPE photoedit_up gauge",
        "photoedit_up 1",
        "# TYPE photoedit_uptime_seconds gauge",
        f"photoedit_uptime_seconds {time.time() - STARTED_AT:.0f}",
        "# TYPE photoedit_users_total gauge",
        f"photoedit_users_total {s['users']}",
        "# TYPE photoedit_credits_remaining gauge",
        f"photoedit_credits_remaining {s['credits']}",
        "# TYPE photoedit_jobs gauge",
    ]
    for key, value in sorted(s.items()):
        if key.startswith("jobs_"):
            lines.append(f'photoedit_jobs{{status="{_prom_label(key[5:])}"}} {value}')
    lines += [
        "# TYPE photoedit_revision_info gauge",
        f'photoedit_revision_info{{revision="{_prom_label(_revision())}"}} 1',
    ]
    return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


@app.get("/api/presets")
def presets():
    return {"presets": [
        {"id": "background", "label": "Background",
         "hint": "New background from your description."},
        {"id": "clothes", "label": "Clothes",
         "hint": "Change clothing from your description."},
        {"id": "restore", "label": "Restore",
         "hint": "Fix scratches, noise, blur, faded colors."},
        {"id": "custom", "label": "Custom",
         "hint": "Free-form instruction (Tagalog/English/Bisaya)."},
    ]}


@app.get("/api/packs")
def packs() -> dict[str, object]:
    # Unconfigured (no keys/URLs): hide the packs instead of offering a 503.
    if not (XENDIT_SECRET_KEY and PAYMENT_SUCCESS_URL and PAYMENT_CANCEL_URL):
        return {"packs": []}
    return {"packs": [{"id": pid, **pack} for pid, pack in PACKS.items()]}


@app.post("/api/checkout")
def checkout(request: Request,
             payload: dict[str, object] = Body(...)) -> dict[str, object]:
    pack_id = str(payload.get("pack") or "").strip()
    user = str(payload.get("user") or "").strip()
    email = str(payload.get("email") or "").strip()
    if pack_id not in PACKS:
        raise HTTPException(400, "unknown pack")
    if not user or len(user) > 100:
        raise HTTPException(400, "user required")
    if not EMAIL_RE.match(email):
        raise HTTPException(400, "valid email required")
    if not (XENDIT_SECRET_KEY and PAYMENT_SUCCESS_URL and PAYMENT_CANCEL_URL):
        raise HTTPException(503, "payments not configured")
    if not _checkout_allowed(_client_ip(request), time.time()):
        raise HTTPException(429, "too many checkout attempts — try again later",
                            headers={"Retry-After": "3600"})
    try:
        session_id, checkout_url = _create_checkout_session(
            pack_id, user, email)
    except XenditError as e:
        logging.getLogger(__name__).warning("checkout failed: %s", e)
        raise HTTPException(502, "could not start checkout — try again") from e
    pack = PACKS[pack_id]
    # Store the amount we actually sent: the webhook compares against it.
    get_store().create_payment(session_id, user, pack_id,
                               pack["credits"],
                               _xendit_amount(pack["price"]), email)
    return {"checkout_url": checkout_url, "session_id": session_id}


@app.post("/api/xendit/webhook")
async def xendit_webhook(request: Request) -> dict[str, object]:
    _verify_webhook_token(request.headers.get("x-callback-token", ""))
    try:
        body = json.loads(await request.body())
        event = body["event"]
        data = body["data"]
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, "bad payload") from None
    if event != "payment_session.completed":
        return {"ok": True, "credited": 0}
    session_id = data.get("payment_session_id")
    record = get_store().payment(session_id) if session_id else None
    if record is None:
        # 200, not 400: Xendit retries non-2xx six times and a retry cannot
        # create a row we never recorded. Log it so a real bug is visible.
        logging.getLogger(__name__).warning(
            "webhook for unknown session: %s", session_id)
        return {"ok": True, "credited": 0}
    if record["status"] == "paid":
        return {"ok": True, "credited": 0}  # duplicate delivery: no-op
    paid_amount = int(data.get("amount") or 0)
    amount_due = record["amount"]
    if not isinstance(amount_due, int) or paid_amount < amount_due:
        raise HTTPException(400, "amount mismatch")
    return {"ok": True, "credited":
            get_store().mark_paid(session_id, paid_amount)}


@app.get("/api/result/{job_id}")
def result(job_id: str, user: str = Depends(current_user)):
    store = get_store()
    job = store.get_job(job_id)
    if job is None:
        raise HTTPException(404, "unknown job")
    expired = artifacts_expired(job)
    base = {"job_id": job_id, "credits_left": store.balance(user),
            "preset": job["preset"], "created_at": job["created_at"],
            "artifacts_expired": expired,
            "inputs": [] if expired else _input_urls(job_id)}
    if base["artifacts_expired"]:
        return {**base, "status": job["status"], "image_url": None}
    if job["status"] != "done":
        return {**base, "status": job["status"], "image_url": None}
    png = STORAGE / job_id / "output.png"
    if not png.exists():
        raise HTTPException(500, "output missing")
    return {**base, "status": "done",
            "image_url": f"/api/result/{job_id}/file"}


_INPUT_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".png": "image/png", ".webp": "image/webp"}


def _input_urls(job_id: str) -> list[str]:
    # Only inputs that actually exist on disk; a job may have fewer than MAX_IMAGES.
    return [f"/api/result/{job_id}/input/{i}" for i in range(MAX_IMAGES)
            if any((STORAGE / job_id / f"input_{i}{ext}").exists()
                   for ext in _INPUT_MIME)]


@app.get("/api/result/{job_id}/input/{index}")
def result_input(job_id: str, index: int, user: str = Depends(current_user)):
    if not 0 <= index < MAX_IMAGES:
        raise HTTPException(404, "input not found")
    store = get_store()
    job = store.get_job(job_id)
    if job is None or job["user"] != user:
        raise HTTPException(404, "unknown job")
    if artifacts_expired(job):
        raise HTTPException(410, "job artifacts expired after seven days")
    job_dir = STORAGE / job_id
    for ext, mime in _INPUT_MIME.items():
        p = job_dir / f"input_{index}{ext}"
        if p.exists():
            return FileResponse(p, media_type=mime)
    raise HTTPException(404, "input not found")


@app.get("/api/jobs")
def jobs(user: str = Depends(current_user)):
    store = get_store()
    items = []
    for j in store.user_jobs(user, limit=30):
        job_id = j["id"]
        expired = artifacts_expired(j)
        image_url = (f"/api/result/{job_id}/file"
                     if j["status"] == "done" and not expired else None)
        inputs = [] if expired else _input_urls(job_id)
        items.append({**j, "job_id": job_id, "inputs": inputs,
                      "image_url": image_url, "artifacts_expired": expired})
    return {"user": user, "fetched_at": time.time(), "jobs": items}


@app.get("/api/result/{job_id}/file")
def result_file(job_id: str, user: str = Depends(current_user)):
    store = get_store()
    job = store.get_job(job_id)
    if job is None or job["user"] != user:
        raise HTTPException(404, "unknown job")
    if artifacts_expired(job):
        raise HTTPException(410, "job artifacts expired after seven days")
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
    image: list[UploadFile] | None = File(None),
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
    # Free-budget gates (global cap + per-device daily) apply only to users
    # who have never paid. A paid balance is finite; the per-minute IP limit,
    # Turnstile and upload validation still apply to everyone.
    paying = store.paid_total(user) > 0 and store.balance(user) > 0
    if not paying and store.count_jobs_since(now - 86400) >= MAX_DAILY_EDITS:
        raise HTTPException(
            503, "today's free budget is used up — try again tomorrow",
            headers={"Retry-After": "3600"})
    if store.ip_job_count_since(ip, now - 60) >= IP_MINUTE_LIMIT:
        raise HTTPException(
            429, "too many tries — wait a minute and try again",
            headers={"Retry-After": "60"})
    if not paying and store.ip_job_count_since(ip, now - 86400) >= IP_DAILY_LIMIT:
        raise HTTPException(
            429, f"this network has used its {IP_DAILY_LIMIT} free runs today",
            headers={"Retry-After": "3600"})
    if not paying and device and store.ip_job_count_since(
            ip, now - 86400, ua=device) >= DEVICE_DAILY_LIMIT:
        raise HTTPException(
            429, f"this device has used its {DEVICE_DAILY_LIMIT} free runs today",
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
    form = await request.form()
    if not image or sum(isinstance(value, StarletteUploadFile)
                        for _, value in form.multi_items()) != len(image):
        raise HTTPException(400, "one to five image uploads required")
    if len(image) > MAX_IMAGES:
        raise HTTPException(400, f"too many photos: max {MAX_IMAGES}")
    images = []
    for photo in image:
        if photo.content_type not in ALLOWED_MIME:
            raise HTTPException(
                400, f"unsupported type {photo.content_type}: jpeg/png/webp only")
        data = await photo.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise HTTPException(400, "file too large: max 5MB")
        if not data:
            raise HTTPException(400, "empty file rejected")
        images.append(data)


    job_id = uuid.uuid4().hex[:12]
    ip_sem = _slots_for(ip)
    if not ip_sem.acquire(blocking=False):
        raise HTTPException(
            429, f"{MAX_PER_IP} concurrent jobs per IP — try again shortly",
            headers={"Retry-After": "10"})
    if not _global_slots.acquire(blocking=False):
        ip_sem.release()
        raise HTTPException(
            429, "server is busy (all job slots full) — try again shortly",
            headers={"Retry-After": "15"})
    debited = False
    try:
        try:
            credits_left = store.check_and_debit(
                user, job_id, preset=preset, ip=ip, ua=device, prompt=prompt,
                daily_limit=None if paying else MAX_DAILY_EDITS,
                since_ts=now - 86400)
        except OutOfCredits:
            ip_sem.release()
            _global_slots.release()
            raise HTTPException(402, "out of credits")
        except DailyLimitReached:
            ip_sem.release()
            _global_slots.release()
            raise HTTPException(
                503, "today's free budget is used up — try again tomorrow",
                headers={"Retry-After": "3600"})

        debited = True
        job_dir = STORAGE / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        ext = {"image/jpeg": ".jpg", "image/png": ".png",
               "image/webp": ".webp"}
        for i, (photo, data) in enumerate(zip(image, images)):
            (job_dir / f"input_{i}{ext[photo.content_type]}").write_bytes(data)

        t = threading.Thread(
            target=_run_job_thread,
            args=(job_id, user, images[0] if len(images) == 1 else images, instruction, ip),
            daemon=True)
        t.start()
    except HTTPException:
        raise
    except Exception as e:
        try:
            if debited:
                store.refund(user, job_id, f"{type(e).__name__}: {e}"[:500])
        finally:
            ip_sem.release()
            _global_slots.release()
        if debited:
            raise HTTPException(502, "could not start edit job") from e
        raise
    return {"job_id": job_id, "status": "running", "credits_left": credits_left,
            "note": FREE_NOTE}


def _run_job_thread(job_id: str, user: str, input_bytes: bytes | list[bytes],
                    instruction: str, slot_key: str) -> None:
    """Background worker (daemon thread): engine -> done | failed+refund."""
    store = get_store()
    try:
        try:
            full_prompt = build_edit_prompt(instruction)
            if isinstance(input_bytes, list):
                full_prompt += ("\nMultiple reference photos are provided in order. "
                                "Use them together as requested in the instruction; "
                                "the first photo is the primary reference.")
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


def _cors_origins() -> set[str]:
    """Extra origins allowed to call /api/* (static frontend on another host)."""
    return {o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()}


@app.middleware("http")
async def _cors(request, call_next):
    origin = request.headers.get("origin", "")
    allowed = origin if origin and origin in _cors_origins() else ""
    if allowed and request.method == "OPTIONS":
        return Response(status_code=204, headers={
            "Access-Control-Allow-Origin": allowed,
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "*",
            "Access-Control-Max-Age": "600",
            "Vary": "Origin",
        })
    response = await call_next(request)
    if allowed:
        response.headers["Access-Control-Allow-Origin"] = allowed
        response.headers["Vary"] = "Origin"
    return response


@app.middleware("http")
async def _revalidate_html(request, call_next):
    """Browsers heuristic-cache HTML, which hides UI changes after a deploy."""
    response = await call_next(request)
    path = request.url.path
    if not path.startswith("/api/") and path.endswith(("/", ".html")):
        response.headers["Cache-Control"] = "no-cache"
    return response


if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
