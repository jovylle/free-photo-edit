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

FREE_NOTE = "3 free edits per user. Outputs up to 1024x1024."

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

# --- Paid edit packs (server-side prices; the browser never sends one) ---
# Amounts are centavos (PHP): ₱49.00 = 4900.
PACKS = {
    "starter": {"label": "Starter", "price": 4900, "credits": 10},
    "plus": {"label": "Plus", "price": 9900, "credits": 25},
    "pro": {"label": "Pro", "price": 19900, "credits": 60},
}
PAYMONGO_API = "https://api.paymongo.com/v2/checkout_sessions"
PAYMONGO_SECRET_KEY = os.environ.get("PAYMONGO_SECRET_KEY", "")
PAYMONGO_WEBHOOK_SECRET = os.environ.get("PAYMONGO_WEBHOOK_SECRET", "")
PAYMENT_SUCCESS_URL = os.environ.get("PAYMENT_SUCCESS_URL", "")
PAYMENT_CANCEL_URL = os.environ.get("PAYMENT_CANCEL_URL", "")
WEBHOOK_MAX_AGE = 300  # seconds; signature replay window
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


class PayMongoError(RuntimeError):
    pass


def _checkout_payload(pack_id: str, user: str, reference: str,
                      email: str) -> dict:
    pack = PACKS[pack_id]
    return {"data": {"attributes": {
        "send_email_receipt": False,
        "show_description": True,
        "show_line_items": True,
        "description": f"Free Photo Edit — {pack['label']}: {pack['credits']} edits",
        "line_items": [{"currency": "PHP", "amount": pack["price"],
                        "name": f"{pack['label']} — {pack['credits']} edits",
                        "quantity": 1}],
        "payment_method_types": ["gcash", "paymaya", "grab_pay", "card"],
        "success_url": PAYMENT_SUCCESS_URL,
        "cancel_url": PAYMENT_CANCEL_URL,
        "reference_number": reference,
        "customer_email": email,
        "metadata": {"user": user, "pack": pack_id},
    }}}


def _create_checkout_session(pack_id: str, user: str,
                             email: str) -> tuple[str, str]:
    """Create a PayMongo Hosted Checkout session -> (session_id, checkout_url)."""
    r = httpx.post(PAYMONGO_API, auth=(PAYMONGO_SECRET_KEY, ""),
                   json=_checkout_payload(pack_id, user, uuid.uuid4().hex,
                                          email),
                   timeout=20)
    if r.status_code not in (200, 201):
        raise PayMongoError(f"PayMongo HTTP {r.status_code}")
    try:
        data = r.json()["data"]
        return data["id"], data["attributes"]["checkout_url"]
    except (KeyError, TypeError):
        raise PayMongoError("PayMongo bad response") from None


def _verify_paymongo_signature(header: str, raw: bytes) -> None:
    """HMAC-SHA256 over f"{t}.{raw_body}", compared to te (test) or li (live)."""
    if not PAYMONGO_WEBHOOK_SECRET:
        raise HTTPException(503, "payments not configured")
    parts = {}
    for item in header.split(","):
        key, _, value = item.strip().partition("=")
        if key and value:
            parts[key] = value
    try:
        ts = int(parts.get("t", ""))
    except ValueError:
        raise HTTPException(401, "bad signature") from None
    if abs(time.time() - ts) > WEBHOOK_MAX_AGE:
        raise HTTPException(401, "stale signature")
    digest = hmac.new(PAYMONGO_WEBHOOK_SECRET.encode(),
                      str(ts).encode() + b"." + raw,
                      hashlib.sha256).hexdigest()
    ok = any(parts.get(k) and hmac.compare_digest(digest, parts[k])
             for k in ("te", "li"))
    if not ok:
        raise HTTPException(401, "bad signature")


def _paid_amount(session: dict) -> int:
    attrs = session.get("attributes") or {}
    return sum(
        int((p.get("attributes") or {}).get("amount") or 0)
        for p in attrs.get("payments") or []
        if (p.get("attributes") or {}).get("status") == "paid"
    )


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
def config():
    return {"turnstile_sitekey": TURNSTILE_SITEKEY or None}


@app.get("/api/presets")
def presets():
    return {"presets": [
        {"id": "background", "label": "🏙 Background",
         "hint": "New background from your description."},
        {"id": "clothes", "label": "👔 Clothes",
         "hint": "Change clothing from your description."},
        {"id": "restore", "label": "✨ Restore",
         "hint": "Fix scratches, noise, blur, faded colors."},
        {"id": "custom", "label": "✏ Custom",
         "hint": "Free-form instruction (Tagalog/English/Bisaya)."},
    ]}


@app.get("/api/packs")
def packs():
    return {"packs": [{"id": pid, **pack} for pid, pack in PACKS.items()]}


@app.post("/api/checkout")
def checkout(request: Request, payload: dict = Body(...)):
    pack_id = str(payload.get("pack") or "").strip()
    user = str(payload.get("user") or "").strip()
    email = str(payload.get("email") or "").strip()
    if pack_id not in PACKS:
        raise HTTPException(400, "unknown pack")
    if not user or len(user) > 100:
        raise HTTPException(400, "user required")
    if not EMAIL_RE.match(email):
        raise HTTPException(400, "valid email required")
    if not (PAYMONGO_SECRET_KEY and PAYMENT_SUCCESS_URL and PAYMENT_CANCEL_URL):
        raise HTTPException(503, "payments not configured")
    if not _checkout_allowed(_client_ip(request), time.time()):
        raise HTTPException(429, "too many checkout attempts — try again later",
                            headers={"Retry-After": "3600"})
    try:
        session_id, checkout_url = _create_checkout_session(
            pack_id, user, email)
    except PayMongoError as e:
        logging.getLogger(__name__).warning("checkout failed: %s", e)
        raise HTTPException(502, "could not start checkout — try again") from e
    pack = PACKS[pack_id]
    get_store().create_payment(session_id, user, pack_id,
                               pack["credits"], pack["price"], email)
    return {"checkout_url": checkout_url, "session_id": session_id}


@app.post("/api/paymongo/webhook")
async def paymongo_webhook(request: Request):
    raw = await request.body()  # HMAC is over raw bytes: read before parsing
    _verify_paymongo_signature(
        request.headers.get("Paymongo-Signature", ""), raw)
    try:
        attrs = json.loads(raw)["data"]["attributes"]
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, "bad payload") from None
    if attrs.get("type") != "checkout_session.payment.paid":
        return {"ok": True, "credited": 0}
    session = attrs.get("data") or {}
    session_id = session.get("id")
    record = get_store().payment(session_id) if session_id else None
    if record is None:
        raise HTTPException(400, "unknown session")
    if record["status"] == "paid":
        return {"ok": True, "credited": 0}  # duplicate delivery: no-op
    paid_amount = _paid_amount(session)
    if paid_amount < record["amount"]:
        raise HTTPException(400, "amount mismatch")
    return {"ok": True, "credited":
            get_store().mark_paid(session_id, paid_amount)}


@app.get("/api/result/{job_id}")
def result(job_id: str, user: str = Depends(current_user)):
    store = get_store()
    job = store.get_job(job_id)
    if job is None:
        raise HTTPException(404, "unknown job")
    base = {"job_id": job_id, "credits_left": store.balance(user),
            "preset": job["preset"], "created_at": job["created_at"],
            "artifacts_expired": artifacts_expired(job)}
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


@app.get("/api/result/{job_id}/input/{index}")
def result_input(job_id: str, index: int):
    if not 0 <= index < MAX_IMAGES:
        raise HTTPException(404, "input not found")
    store = get_store()
    job = store.get_job(job_id)
    if job is None:
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
        inputs = [] if expired else [f"/api/result/{job_id}/input/{i}" for i in range(MAX_IMAGES)
                  if any((STORAGE / job_id / f"input_{i}{ext}").exists()
                         for ext in _INPUT_MIME)]
        items.append({**j, "job_id": job_id, "inputs": inputs,
                      "image_url": image_url, "artifacts_expired": expired})
    return {"user": user, "fetched_at": time.time(), "jobs": items}


@app.get("/api/result/{job_id}/file")
def result_file(job_id: str):
    store = get_store()
    job = store.get_job(job_id)
    if job is None:
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
    if not paying and store.ip_job_count_since(ip, now - 86400, ua=device) >= IP_DAILY_LIMIT:
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
