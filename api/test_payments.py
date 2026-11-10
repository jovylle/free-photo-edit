"""Paid edit packs: checkout, webhook fulfilment, ledger idempotency, gates.

No network: PayMongo HTTP is monkeypatched and signatures are computed locally.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from free_photo_edit_api import server
from free_photo_edit_api.credits import CreditsStore

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
    b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)
SECRET = "whsk_test_secret"


class FakeEngine:
    name = "fake"
    calls = 0

    def edit(self, input_bytes, prompt):
        type(self).calls += 1
        return PNG


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = str(tmp_path / "credits.db")
    monkeypatch.setenv("FREE_PHOTO_EDIT_DB", db)
    monkeypatch.setenv("FREE_PHOTO_EDIT_STORAGE", str(tmp_path / "storage"))
    server.STORAGE = tmp_path / "storage"
    server.DB_PATH = db
    server.reset_store_for_tests(db)
    server.reset_slots_for_tests()
    monkeypatch.setattr(server, "select_engine", lambda: FakeEngine())
    monkeypatch.setattr(server, "TURNSTILE_SECRET", "")
    monkeypatch.setattr(server, "PAYMONGO_SECRET_KEY", "sk_test_x")
    monkeypatch.setattr(server, "PAYMONGO_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(server, "PAYMENT_SUCCESS_URL", "https://fpe.test/?paid=1")
    monkeypatch.setattr(server, "PAYMENT_CANCEL_URL", "https://fpe.test/")
    server._checkout_hits.clear()
    FakeEngine.calls = 0
    return TestClient(server.app)


def _fake_paymongo(monkeypatch, captured=None, status_code=200):
    captured = captured if captured is not None else {}
    counter = {"n": 0}

    def post(url, **kw):
        counter["n"] += 1
        captured.update(url=url, **kw)
        return _Resp(status_code, {"data": {
            "id": f"cs_test_{counter['n']}",
            "attributes": {"checkout_url": "https://checkout.paymongo/x"}}})
    monkeypatch.setattr(server.httpx, "post", post)
    return captured


def _pending(session_id="cs_test_1", user="buyer", credits=10, amount=4900):
    server.get_store().create_payment(
        session_id, user, "starter", credits, amount)


def _sign(raw: bytes, t: int, key="te", secret=SECRET) -> str:
    digest = hmac.new(secret.encode(), str(t).encode() + b"." + raw,
                      hashlib.sha256).hexdigest()
    return f"t={t},{key}={digest}"


def _paid_event(session_id="cs_test_1", amount=4900, kind=None):
    return {"data": {"attributes": {
        "type": kind or "checkout_session.payment.paid",
        "data": {"id": session_id, "attributes": {"payments": [
            {"attributes": {"amount": amount, "status": "paid"}}]}}}}}


def _webhook(client, body, t=None, key="te", secret=SECRET):
    raw = json.dumps(body).encode()
    t = int(t if t is not None else time.time())
    return client.post("/api/paymongo/webhook", content=raw,
                       headers={"Paymongo-Signature": _sign(raw, t, key, secret)})


def _edit(client, user, ip="9.9.9.9", ua="paid-ua"):
    return client.post(
        "/api/edit",
        headers={"X-User": user, "CF-Connecting-IP": ip, "User-Agent": ua},
        data={"prompt": "fix it", "preset": "custom", "user": user},
        files={"image": ("in.png", PNG, "image/png")},
    )


def _dev(ip, ua):
    return hashlib.sha256(f"{ip}|{ua}".encode()).hexdigest()[:32]


def _seed(store, n, ip="1.1.1.1", ua="", prefix="seed"):
    for _ in range(n):
        store.check_and_debit(f"{prefix}-{uuid.uuid4().hex[:8]}",
                              uuid.uuid4().hex[:12], ip=ip, ua=ua)


def _buy(store, user="buyer", session="cs_paid_1", credits=10):
    store.create_payment(session, user, "starter", credits, 4900)
    assert store.mark_paid(session, 4900) == credits


# --- checkout ---


def test_packs_are_server_side(client):
    packs = {p["id"]: p for p in client.get("/api/packs").json()["packs"]}
    assert packs["starter"] == {"id": "starter", "label": "Starter",
                                "price": 4900, "credits": 10}
    assert packs["plus"]["price"] == 9900 and packs["plus"]["credits"] == 25
    assert packs["pro"]["price"] == 19900 and packs["pro"]["credits"] == 60


def test_checkout_sends_expected_payload_and_records_pending(client, monkeypatch):
    captured = _fake_paymongo(monkeypatch)
    r = client.post("/api/checkout", json={"pack": "starter", "user": "u1",
                                           "email": "buyer@example.com"})
    assert r.status_code == 200, r.text
    assert r.json()["checkout_url"] == "https://checkout.paymongo/x"
    body = captured["json"]["data"]["attributes"]
    assert body["line_items"] == [{"currency": "PHP", "amount": 4900,
                                   "name": "Starter — 10 edits", "quantity": 1}]
    assert body["metadata"] == {"user": "u1", "pack": "starter"}
    assert body["customer_email"] == "buyer@example.com"
    assert body["success_url"] == "https://fpe.test/?paid=1"
    assert captured["auth"] == ("sk_test_x", "")
    record = server.get_store().payment(r.json()["session_id"])
    assert record["status"] == "pending"
    assert record["credits"] == 10 and record["amount"] == 4900
    assert record["email"] == "buyer@example.com"


@pytest.mark.parametrize("email", ["", "not-an-email", "a b@example.com"])
def test_checkout_requires_valid_email(client, monkeypatch, email):
    monkeypatch.setattr(server.httpx, "post",
                        lambda *a, **k: pytest.fail("PayMongo must not be called"))
    r = client.post("/api/checkout", json={"pack": "starter", "user": "u1",
                                           "email": email})
    assert r.status_code == 400


def test_checkout_unknown_pack_400(client, monkeypatch):
    monkeypatch.setattr(server.httpx, "post",
                        lambda *a, **k: pytest.fail("PayMongo must not be called"))
    r = client.post("/api/checkout", json={"pack": "nope", "user": "u1",
                                           "email": "buyer@example.com"})
    assert r.status_code == 400


def test_checkout_unset_keys_503(client, monkeypatch):
    monkeypatch.setattr(server, "PAYMONGO_SECRET_KEY", "")
    r = client.post("/api/checkout", json={"pack": "starter", "user": "u1",
                                           "email": "buyer@example.com"})
    assert r.status_code == 503


def test_checkout_rate_limit_429(client, monkeypatch):
    _fake_paymongo(monkeypatch)
    monkeypatch.setattr(server, "CHECKOUT_PER_HOUR", 2)
    body = {"pack": "starter", "user": "u1", "email": "buyer@example.com"}
    for _ in range(2):
        assert client.post("/api/checkout", json=body).status_code == 200
    assert client.post("/api/checkout", json=body).status_code == 429


# --- webhook ---


def test_webhook_credits_once(client):
    _pending()
    r = _webhook(client, _paid_event())
    assert r.status_code == 200, r.text
    assert r.json()["credited"] == 10
    store = server.get_store()
    assert store.balance("buyer") == 13  # 3 free + 10 bought
    assert store.paid_total("buyer") == 10
    assert store.payment("cs_test_1")["status"] == "paid"


def test_webhook_duplicate_delivery_is_noop(client):
    _pending()
    assert _webhook(client, _paid_event()).status_code == 200
    r = _webhook(client, _paid_event())
    assert r.status_code == 200 and r.json()["credited"] == 0
    assert server.get_store().balance("buyer") == 13


def test_webhook_bad_signature_401(client):
    _pending()
    raw = json.dumps(_paid_event()).encode()
    r = client.post("/api/paymongo/webhook", content=raw,
                    headers={"Paymongo-Signature":
                             _sign(raw, int(time.time()), secret="wrong")})
    assert r.status_code == 401
    assert server.get_store().balance("buyer") == 3


def test_webhook_stale_timestamp_401(client):
    _pending()
    assert _webhook(client, _paid_event(),
                    t=int(time.time()) - 600).status_code == 401
    assert server.get_store().balance("buyer") == 3


def test_webhook_unknown_session_400(client):
    assert _webhook(client, _paid_event("cs_nope")).status_code == 400


def test_webhook_amount_below_expected_400(client):
    _pending()
    assert _webhook(client, _paid_event(amount=4800)).status_code == 400
    store = server.get_store()
    assert store.balance("buyer") == 3
    assert store.payment("cs_test_1")["status"] == "pending"


def test_webhook_other_event_type_is_noop(client):
    _pending()
    r = _webhook(client, _paid_event(kind="payment.paid"))
    assert r.status_code == 200 and r.json()["credited"] == 0
    assert server.get_store().balance("buyer") == 3


def test_webhook_live_signature_accepted(client):
    _pending()
    assert _webhook(client, _paid_event(), key="li").status_code == 200
    assert server.get_store().balance("buyer") == 13


def test_webhook_helper_signature_matches_server(monkeypatch):
    """The sandbox helper must produce headers the server accepts."""
    import importlib.util
    from pathlib import Path

    path = (Path(__file__).resolve().parent.parent
            / "scripts" / "paymongo_webhook_test.py")
    spec = importlib.util.spec_from_file_location("paymongo_webhook_test", path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    monkeypatch.setattr(server, "PAYMONGO_WEBHOOK_SECRET", SECRET)
    raw = json.dumps(helper.event("cs_x", 4900)).encode()
    t = int(time.time())
    server._verify_paymongo_signature(
        f"t={t},te={helper.sign(SECRET, t, raw)}", raw)  # must not raise
    with pytest.raises(server.HTTPException):
        server._verify_paymongo_signature(
            f"t={t},te={helper.sign('wrong', t, raw)}", raw)


# --- ledger ---


def test_mark_paid_is_atomic_and_idempotent(tmp_path):
    store = CreditsStore(str(tmp_path / "c.db"))
    store.create_payment("cs_1", "u", "starter", 10, 4900)
    assert store.mark_paid("cs_1", 4900) == 10
    assert store.balance("u") == 13
    assert store.paid_total("u") == 10
    assert store.mark_paid("cs_1", 4900) == 0
    assert store.balance("u") == 13
    assert store.paid_total("u") == 10
    with pytest.raises(KeyError):
        store.mark_paid("cs_nope", 4900)


def test_mark_paid_below_price_credits_nothing(tmp_path):
    store = CreditsStore(str(tmp_path / "c.db"))
    store.create_payment("cs_2", "u", "starter", 10, 4900)
    assert store.mark_paid("cs_2", 4899) == 0
    assert store.balance("u") == 3
    assert store.payment("cs_2")["status"] == "pending"


# --- abuse gates must not punish buyers ---


def test_paid_user_bypasses_global_daily_cap(client, monkeypatch):
    monkeypatch.setattr(server, "MAX_DAILY_EDITS", 1)
    store = server.get_store()
    _seed(store, 1)  # free budget full
    _buy(store, "paying")
    r = _edit(client, "paying", ip="8.8.8.8")
    assert r.status_code == 202, r.text
    assert store.balance("paying") == 12


def test_paid_user_bypasses_device_daily_limit(client, monkeypatch):
    monkeypatch.setattr(server, "IP_DAILY_LIMIT", 1)
    store = server.get_store()
    _buy(store, "paying")
    _seed(store, 1, ip="8.8.8.8", ua=_dev("8.8.8.8", "paid-ua"))
    r = _edit(client, "paying", ip="8.8.8.8")
    assert r.status_code == 202, r.text


def test_unpaid_user_still_capped(client, monkeypatch):
    monkeypatch.setattr(server, "MAX_DAILY_EDITS", 1)
    _seed(server.get_store(), 1)
    r = _edit(client, "free-user", ip="8.8.8.8", ua="free-ua")
    assert r.status_code == 503
    assert FakeEngine.calls == 0


def test_paid_user_with_zero_balance_still_402(client):
    store = server.get_store()
    _buy(store, "spent")
    store.set_credits("spent", 0)
    r = _edit(client, "spent", ip="8.8.8.8")
    assert r.status_code == 402
    assert FakeEngine.calls == 0
