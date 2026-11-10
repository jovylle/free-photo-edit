"""Paid edit packs: checkout, webhook fulfilment, ledger idempotency, gates.

No network: Xendit HTTP is monkeypatched and the callback token is local.
"""

from __future__ import annotations

import hashlib
import json
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
TOKEN = "whsk_test_token"
SECRET = "xnd_development_test"


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
    monkeypatch.setattr(server, "XENDIT_SECRET_KEY", SECRET)
    monkeypatch.setattr(server, "XENDIT_WEBHOOK_TOKEN", TOKEN)
    monkeypatch.setattr(server, "PAYMENT_SUCCESS_URL", "https://fpe.test/?paid=1")
    monkeypatch.setattr(server, "PAYMENT_CANCEL_URL", "https://fpe.test/")
    server._checkout_hits.clear()
    FakeEngine.calls = 0
    return TestClient(server.app)


def _fake_xendit(monkeypatch, captured=None, status_code=200):
    captured = captured if captured is not None else {}
    counter = {"n": 0}

    def post(url, **kw):
        counter["n"] += 1
        captured.update(url=url, **kw)
        return _Resp(status_code, {
            "payment_session_id": f"ps_test_{counter['n']}",
            "payment_link_url": "https://checkout.xendit/x"})
    monkeypatch.setattr(server.httpx, "post", post)
    return captured


def _pending(session_id="ps_test_1", user="buyer", credits=10, amount=49):
    server.get_store().create_payment(
        session_id, user, "starter", credits, amount)


def _paid_event(session_id="ps_test_1", amount=49,
                kind="payment_session.completed"):
    return {"event": kind, "data": {
        "payment_session_id": session_id, "amount": amount,
        "currency": "PHP", "status": "COMPLETED"}}


def _webhook(client, body, token=TOKEN):
    return client.post("/api/xendit/webhook", content=json.dumps(body).encode(),
                       headers={"x-callback-token": token})


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


def _buy(store, user="buyer", session="ps_paid_1", credits=10):
    store.create_payment(session, user, "starter", credits, 49)
    assert store.mark_paid(session, 49) == credits


# --- checkout ---


def test_packs_hidden_when_unconfigured(client, monkeypatch):
    monkeypatch.setattr(server, "XENDIT_SECRET_KEY", "")
    assert client.get("/api/packs").json() == {"packs": []}


def test_packs_are_server_side(client):
    packs = {p["id"]: p for p in client.get("/api/packs").json()["packs"]}
    assert packs["starter"] == {"id": "starter", "label": "Starter",
                                "price": 4900, "credits": 10}
    assert packs["plus"]["price"] == 9900 and packs["plus"]["credits"] == 25
    assert packs["pro"]["price"] == 19900 and packs["pro"]["credits"] == 60


def test_checkout_sends_expected_payload_and_records_pending(client, monkeypatch):
    captured = _fake_xendit(monkeypatch)
    r = client.post("/api/checkout", json={"pack": "starter", "user": "u1",
                                           "email": "buyer@example.com"})
    assert r.status_code == 200, r.text
    assert r.json()["checkout_url"] == "https://checkout.xendit/x"
    body = captured["json"]
    # Whole pesos: the hosted page must show ₱49.00, not ₱4,900.
    assert body["amount"] == 49
    assert body["currency"] == "PHP" and body["country"] == "PH"
    assert body["session_type"] == "PAY" and body["mode"] == "PAYMENT_LINK"
    assert body["items"][0]["net_unit_amount"] == 49
    assert body["metadata"]["user"] == "u1"
    assert body["metadata"]["pack"] == "starter"
    assert body["success_return_url"] == "https://fpe.test/?paid=1"
    assert captured["auth"] == (SECRET, "")
    record = server.get_store().payment(r.json()["session_id"])
    assert record["status"] == "pending"
    assert record["credits"] == 10 and record["amount"] == 49
    assert record["email"] == "buyer@example.com"


@pytest.mark.parametrize("email", ["", "not-an-email", "a b@example.com"])
def test_checkout_requires_valid_email(client, monkeypatch, email):
    monkeypatch.setattr(server.httpx, "post",
                        lambda *a, **k: pytest.fail("Xendit must not be called"))
    r = client.post("/api/checkout", json={"pack": "starter", "user": "u1",
                                           "email": email})
    assert r.status_code == 400


def test_checkout_unknown_pack_400(client, monkeypatch):
    monkeypatch.setattr(server.httpx, "post",
                        lambda *a, **k: pytest.fail("Xendit must not be called"))
    r = client.post("/api/checkout", json={"pack": "nope", "user": "u1",
                                           "email": "buyer@example.com"})
    assert r.status_code == 400


def test_checkout_unset_keys_503(client, monkeypatch):
    monkeypatch.setattr(server, "XENDIT_SECRET_KEY", "")
    r = client.post("/api/checkout", json={"pack": "starter", "user": "u1",
                                           "email": "buyer@example.com"})
    assert r.status_code == 503


def test_checkout_rate_limit_429(client, monkeypatch):
    _fake_xendit(monkeypatch)
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
    assert store.payment("ps_test_1")["status"] == "paid"


def test_webhook_duplicate_delivery_is_noop(client):
    _pending()
    assert _webhook(client, _paid_event()).status_code == 200
    r = _webhook(client, _paid_event())
    assert r.status_code == 200 and r.json()["credited"] == 0
    assert server.get_store().balance("buyer") == 13


def test_webhook_bad_token_401(client):
    _pending()
    assert _webhook(client, _paid_event(), token="wrong").status_code == 401
    assert _webhook(client, _paid_event(), token="").status_code == 401
    assert server.get_store().balance("buyer") == 3


def test_webhook_unknown_session_is_noop(client):
    r = _webhook(client, _paid_event("ps_nope"))
    assert r.status_code == 200 and r.json()["credited"] == 0


def test_webhook_amount_below_expected_400(client):
    _pending()
    assert _webhook(client, _paid_event(amount=48)).status_code == 400
    store = server.get_store()
    assert store.balance("buyer") == 3
    assert store.payment("ps_test_1")["status"] == "pending"


def test_webhook_other_event_type_is_noop(client):
    _pending()
    r = _webhook(client, _paid_event(kind="payment_session.expired"))
    assert r.status_code == 200 and r.json()["credited"] == 0
    assert server.get_store().balance("buyer") == 3


def test_webhook_unset_token_503(client, monkeypatch):
    _pending()
    monkeypatch.setattr(server, "XENDIT_WEBHOOK_TOKEN", "")
    assert _webhook(client, _paid_event()).status_code == 503


def test_webhook_helper_token_matches_server(monkeypatch):
    """The sandbox helper must send a token the server accepts."""
    import importlib.util
    from pathlib import Path

    path = (Path(__file__).resolve().parent.parent
            / "scripts" / "xendit_webhook_test.py")
    spec = importlib.util.spec_from_file_location("xendit_webhook_test", path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    monkeypatch.setattr(server, "XENDIT_WEBHOOK_TOKEN", TOKEN)
    server._verify_webhook_token(TOKEN)  # must not raise
    with pytest.raises(server.HTTPException):
        server._verify_webhook_token("wrong")
    assert helper.event("ps_x", 49)["data"]["amount"] == 49


# --- ledger ---


def test_mark_paid_is_atomic_and_idempotent(tmp_path):
    store = CreditsStore(str(tmp_path / "c.db"))
    store.create_payment("ps_1", "u", "starter", 10, 49)
    assert store.mark_paid("ps_1", 49) == 10
    assert store.balance("u") == 13
    assert store.paid_total("u") == 10
    assert store.mark_paid("ps_1", 49) == 0
    assert store.balance("u") == 13
    assert store.paid_total("u") == 10
    with pytest.raises(KeyError):
        store.mark_paid("ps_nope", 49)


def test_mark_paid_below_price_credits_nothing(tmp_path):
    store = CreditsStore(str(tmp_path / "c.db"))
    store.create_payment("ps_2", "u", "starter", 10, 49)
    assert store.mark_paid("ps_2", 48) == 0
    assert store.balance("u") == 3
    assert store.payment("ps_2")["status"] == "pending"


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
