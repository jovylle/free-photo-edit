"""Abuse controls: Turnstile gate, IP minute/daily limits, global daily cap.

Rolling windows (no midnight logic). All gates run BEFORE slots/debit, so
rejected requests cost zero credits and zero upstream calls.
"""

from __future__ import annotations

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


class FakeEngine:
    name = "fake"
    calls = 0

    def edit(self, input_bytes, prompt):
        type(self).calls += 1
        return PNG


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
    FakeEngine.calls = 0
    return TestClient(server.app)


def _edit(client, user, ip="9.9.9.9", token=None, ua="test-ua"):
    data = {"prompt": "fix it", "preset": "custom", "user": user}
    if token is not None:
        data["cf-turnstile-response"] = token
    return client.post(
        "/api/edit", headers={"X-User": user, "CF-Connecting-IP": ip,
                              "User-Agent": ua},
        data=data, files={"image": ("in.png", PNG, "image/png")},
    )


def _dev(ip, ua="test-ua"):
    """Device key exactly as server._client_device computes it."""
    import hashlib
    return hashlib.sha256(f"{ip}|{ua}".encode()).hexdigest()[:32]


def _seed(store, n, ip="9.9.9.9", prefix="seed", ua=""):
    for _ in range(n):
        store.check_and_debit(f"{prefix}-{uuid.uuid4().hex[:8]}",
                              uuid.uuid4().hex[:12], ip=ip, ua=ua)


# --- Turnstile gate ---


def test_turnstile_skipped_without_secret(client):
    r = _edit(client, "dev1", token=None)
    assert r.status_code == 202, r.text


def test_turnstile_missing_token_403_zero_spend(client, monkeypatch):
    # Empty token is rejected by the real verifier before any network call.
    monkeypatch.setattr(server, "TURNSTILE_SECRET", "s3cr3t")
    r = _edit(client, "bot1", token=None)
    assert r.status_code == 403, r.text
    assert FakeEngine.calls == 0
    assert server.get_store().balance("bot1") == 3


def test_turnstile_rejected_token_403_zero_spend(client, monkeypatch):
    monkeypatch.setattr(server, "TURNSTILE_SECRET", "s3cr3t")

    def _nope(token, ip):
        raise server.TurnstileError("token rejected")
    monkeypatch.setattr(server, "_verify_turnstile", _nope)
    r = _edit(client, "bot2", token="fake-token")
    assert r.status_code == 403, r.text
    assert FakeEngine.calls == 0
    assert server.get_store().balance("bot2") == 3


def test_turnstile_accepted_token_202(client, monkeypatch):
    monkeypatch.setattr(server, "TURNSTILE_SECRET", "s3cr3t")
    monkeypatch.setattr(server, "_verify_turnstile", lambda token, ip: None)
    r = _edit(client, "human1", token="good-token")
    assert r.status_code == 202, r.text
    assert FakeEngine.calls == 1


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload


def test_verify_accepts_exact_match(monkeypatch):
    monkeypatch.setattr(server, "TURNSTILE_SECRET", "s3cr3t")
    monkeypatch.setattr(server, "TURNSTILE_HOSTNAMES", {"freephoto.uft1.com"})
    monkeypatch.setattr(
        server.httpx, "post",
        lambda url, data=None, timeout=None: _Resp(200, {
            "success": True, "action": "edit",
            "hostname": "freephoto.uft1.com"}),
    )
    server._verify_turnstile("tok", "1.1.1.1")  # must not raise


@pytest.mark.parametrize("payload,status", [
    ({"success": False}, 200),                       # bot / bad token
    ({"success": True, "action": "login",
      "hostname": "freephoto.uft1.com"}, 200),       # wrong action
    ({"success": True, "action": "edit",
      "hostname": "evil.example"}, 200),             # wrong hostname
    ({"success": True}, 500),                        # upstream error
])
def test_verify_rejects(monkeypatch, payload, status):
    monkeypatch.setattr(server, "TURNSTILE_SECRET", "s3cr3t")
    monkeypatch.setattr(server, "TURNSTILE_HOSTNAMES", {"freephoto.uft1.com"})
    monkeypatch.setattr(server.httpx, "post",
                         lambda *a, **k: _Resp(status, payload))
    with pytest.raises(server.TurnstileError):
        server._verify_turnstile("tok", "1.1.1.1")


def test_verify_unreachable_fails_closed(monkeypatch):
    monkeypatch.setattr(server, "TURNSTILE_SECRET", "s3cr3t")
    monkeypatch.setattr(server, "TURNSTILE_HOSTNAMES", {"freephoto.uft1.com"})

    def _down(*a, **k):
        raise TimeoutError("no route")
    monkeypatch.setattr(server.httpx, "post", _down)
    with pytest.raises(server.TurnstileError):
        server._verify_turnstile("tok", "1.1.1.1")


# --- IP + global rate limits ---


def test_ip_minute_limit_429_zero_spend(client, monkeypatch):
    monkeypatch.setattr(server, "IP_MINUTE_LIMIT", 3)
    for i in range(3):
        r = _edit(client, f"m{i}", ip="5.5.5.5", ua=f"ua-{i}")
        assert r.status_code == 202, r.text
    r = _edit(client, "m3", ip="5.5.5.5", ua="ua-3")
    assert r.status_code == 429, r.text
    assert FakeEngine.calls == 3
    assert server.get_store().balance("m3") == 3


def test_ip_daily_limit_ignores_user_agent(client, monkeypatch):
    # Same IP, different UA -> the per-IP ceiling still applies.
    monkeypatch.setattr(server, "IP_MINUTE_LIMIT", 999)
    monkeypatch.setattr(server, "IP_DAILY_LIMIT", 2)
    for i in range(2):
        r = _edit(client, f"d{i}", ip="6.6.6.6", ua=f"device-{i}")
        assert r.status_code == 202, r.text
    r = _edit(client, "d-a", ip="6.6.6.6", ua="device-a")
    assert r.status_code == 429, r.text
    assert "network" in r.json()["detail"]


def test_device_daily_limit_429(client, monkeypatch):
    # Shared WiFi: one device's bucket fills before the per-IP ceiling.
    monkeypatch.setattr(server, "IP_MINUTE_LIMIT", 999)
    monkeypatch.setattr(server, "IP_DAILY_LIMIT", 999)
    monkeypatch.setattr(server, "DEVICE_DAILY_LIMIT", 2)
    _seed(server.get_store(), 2, ip="7.7.7.7",
          ua=_dev("7.7.7.7", "friend-x"), prefix="fx")
    calls_before = FakeEngine.calls
    r = _edit(client, "fx1", ip="7.7.7.7", ua="friend-x")
    assert r.status_code == 429, r.text
    assert "device" in r.json()["detail"]
    assert FakeEngine.calls == calls_before


def test_shared_wifi_devices_separate_buckets(client, monkeypatch):
    monkeypatch.setattr(server, "IP_MINUTE_LIMIT", 999)
    monkeypatch.setattr(server, "IP_DAILY_LIMIT", 999)
    monkeypatch.setattr(server, "DEVICE_DAILY_LIMIT", 1)
    assert _edit(client, "w1", ip="6.6.6.6", ua="friend-a").status_code == 202
    assert _edit(client, "w2", ip="6.6.6.6", ua="friend-b").status_code == 202
    r = _edit(client, "w3", ip="6.6.6.6", ua="friend-a")
    assert r.status_code == 429, r.text


def test_global_daily_cap_503(client, monkeypatch):
    monkeypatch.setattr(server, "MAX_DAILY_EDITS", 2)
    _seed(server.get_store(), 2, ip="7.7.7.7")
    r = _edit(client, "g1", ip="8.8.8.8")
    assert r.status_code == 503, r.text
    assert FakeEngine.calls == 0


def test_global_daily_cap_is_rechecked_atomically(client, monkeypatch):
    monkeypatch.setattr(server, "MAX_DAILY_EDITS", 1)
    store = server.get_store()
    _seed(store, 1)
    balance_before = store.balance("racer")
    # Simulate another process crossing the cap after the route's fast check.
    monkeypatch.setattr(store, "count_jobs_since", lambda since_ts: 0)

    response = _edit(client, "racer", ip="8.8.8.8")

    assert response.status_code == 503, response.text
    assert FakeEngine.calls == 0
    assert store.balance("racer") == balance_before


def test_store_windows_exclude_old_jobs(tmp_path):
    store = CreditsStore(str(tmp_path / "w.db"))
    store.check_and_debit("fresh", "job-new", ip="1.2.3.4",
                          ua=_dev("1.2.3.4"))
    with store._lock:
        store._db.execute(
            "INSERT INTO jobs(id, user, status, created_at, preset, ip, ua)"
            " VALUES(?,?,?,?,?,?,?)",
            ("job-old", "old", "done", time.time() - 90000,
             "custom", "1.2.3.4", _dev("1.2.3.4")),
        )
        store._db.commit()
    assert store.ip_job_count_since("1.2.3.4", time.time() - 60) == 1
    assert store.ip_job_count_since("1.2.3.4", time.time() - 86400) == 1
    assert store.ip_job_count_since(
        "1.2.3.4", time.time() - 60, ua=_dev("1.2.3.4")) == 1
    assert store.count_jobs_since(time.time() - 86400) == 1


def test_config_reports_sitekey(client, monkeypatch):
    monkeypatch.setattr(server, "TURNSTILE_SITEKEY", "")  # hermetic vs real .env
    assert client.get("/api/config").json()["turnstile_sitekey"] is None
    monkeypatch.setattr(server, "TURNSTILE_SITEKEY", "0xKEY")
    assert client.get("/api/config").json()["turnstile_sitekey"] == "0xKEY"
