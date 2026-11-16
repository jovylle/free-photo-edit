"""Analytics beacons: allowlist, UTC day buckets, aggregate-only stats,
privacy (nothing identifying reaches the table) and the per-device limit."""

from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from api import server
from api.events import ALLOWED_EVENTS, EventsStore

DEVICE_A = {"CF-Connecting-IP": "9.9.9.9", "User-Agent": "device-a"}
DEVICE_B = {"CF-Connecting-IP": "9.9.9.9", "User-Agent": "device-b"}


@pytest.fixture()
def store(tmp_path: Path) -> EventsStore:
    return EventsStore(str(tmp_path / "credits.db"))


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    db = str(tmp_path / "credits.db")
    monkeypatch.setenv("FREE_PHOTO_EDIT_DB", db)
    monkeypatch.setenv("FREE_PHOTO_EDIT_STORAGE", str(tmp_path / "storage"))
    server.STORAGE = tmp_path / "storage"
    server.DB_PATH = db
    server.reset_store_for_tests(db)
    server.reset_events_for_tests(db)
    monkeypatch.setattr(server, "EVENT_MINUTE_LIMIT", 60)
    return TestClient(server.app)


def _post(client: TestClient, payload: object,
          headers: dict[str, str] = DEVICE_A) -> httpx.Response:
    response: httpx.Response = client.post(
        "/api/event", json=payload, headers=headers)
    return response


def _rows(store: EventsStore) -> list[tuple[object, ...]]:
    with store._lock:
        return store._db.execute("SELECT * FROM events").fetchall()


# --- allowlist ---


def test_event_accepts_every_allowlisted_name(client: TestClient) -> None:
    for name in sorted(ALLOWED_EVENTS):
        assert _post(client, {"name": name}).status_code == 204
    funnel = server.get_events().stats(30)["funnel"]
    assert set(funnel) == set(ALLOWED_EVENTS)
    assert all(funnel[name] == 1 for name in ALLOWED_EVENTS)


def test_event_rejects_unknown_name_without_storing(client: TestClient) -> None:
    r = _post(client, {"name": "email_capture"})
    assert r.status_code == 400
    assert sum(server.get_events().stats(30)["funnel"].values()) == 0


def test_event_rejects_missing_name(client: TestClient) -> None:
    assert _post(client, {"path": "/"}).status_code == 400


def test_event_rejects_malformed_or_non_object_body(client: TestClient) -> None:
    assert client.post("/api/event", content=b"{not json").status_code == 400
    assert _post(client, ["page_view"]).status_code == 400


def test_event_rejects_body_over_one_kilobyte(client: TestClient) -> None:
    r = _post(client, {"name": "page_view", "path": "/" + "a" * 1100})
    assert r.status_code == 413
    assert _rows(server.get_events()) == []


def test_unknown_preset_is_dropped(client: TestClient) -> None:
    assert _post(client, {"name": "submit", "preset": "my private words"}).status_code == 204
    assert [row[3] for row in _rows(server.get_events())] == [""]


# --- storage ---


def test_events_live_in_the_credits_database(client: TestClient) -> None:
    assert _post(client, {"name": "page_view"}).status_code == 204
    ledger = server.get_store()
    with ledger._lock:
        tables = {str(row[0]) for row in ledger._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"events", "users", "jobs"} <= tables


def test_no_ip_prompt_email_or_user_agent_is_stored(client: TestClient) -> None:
    r = client.post(
        "/api/event",
        headers={"CF-Connecting-IP": "203.0.113.9",
                 "User-Agent": "SecretAgent/1.0"},
        json={"name": "submit", "path": "/", "preset": "custom",
              "prompt": "my private instruction", "ip": "203.0.113.9",
              "email": "someone@example.com"},
    )
    assert r.status_code == 204
    store = server.get_events()
    with store._lock:
        columns = [row[1] for row in store._db.execute(
            "PRAGMA table_info(events)").fetchall()]
    assert columns == ["id", "name", "path", "preset", "device", "day", "ts"]
    rows = _rows(store)
    blob = " ".join(str(value) for row in rows for value in row)
    assert "203.0.113.9" not in blob
    assert "SecretAgent" not in blob
    assert "my private instruction" not in blob
    assert "someone@example.com" not in blob
    assert len(str(rows[0][4])) == 16  # device is a truncated salted hash


# --- aggregation ---


def test_stats_bucket_events_by_utc_day(store: EventsStore) -> None:
    now = time.time()
    store.record("page_view", ts=now)
    store.record("page_view", ts=now - 86400)
    store.record("submit", preset="background", ts=now - 86400)
    counts = {d["day"]: d["counts"] for d in store.stats(30)["daily"]}
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    yesterday = time.strftime("%Y-%m-%d", time.gmtime(now - 86400))
    assert counts[today]["page_view"] == 1
    assert counts[yesterday]["page_view"] == 1
    assert counts[yesterday]["submit"] == 1


def test_day_column_is_the_utc_date(store: EventsStore) -> None:
    store.record("upload", ts=1_700_000_000)  # 2023-11-14T22:13:20Z
    with store._lock:
        days = [row[0] for row in store._db.execute("SELECT day FROM events")]
    assert days == ["2023-11-14"]


def test_stats_shape_and_success_rate(store: EventsStore) -> None:
    store.record("page_view")
    store.record("upload")
    store.record("submit", preset="background")
    store.record("success", preset="background")
    store.record("fail", preset="restore")
    store.record("donate_click")
    data = store.stats(30)
    assert data["days"] == 30
    assert set(data["funnel"]) == set(ALLOWED_EVENTS)
    assert data["funnel"]["page_view"] == 1
    assert data["funnel"]["correction"] == 0   # zero-filled, not missing
    assert data["funnel"]["pack_view"] == 0
    assert data["success_rate"] == 0.5
    assert data["presets"][0]["preset"] == "background"
    assert data["presets"][0]["count"] == 2
    assert data["daily"][-1]["counts"]["success"] == 1


def test_success_rate_is_zero_without_results(store: EventsStore) -> None:
    store.record("page_view")
    assert store.stats(1)["success_rate"] == 0.0


def test_stats_days_are_clamped(client: TestClient) -> None:
    assert client.get("/api/stats?days=0").json()["days"] == 1
    assert client.get("/api/stats?days=9999").json()["days"] == 365
    assert client.get("/api/stats").json()["days"] == 30


def test_stats_response_carries_no_device_or_ip_data(client: TestClient) -> None:
    assert _post(client, {"name": "submit", "preset": "custom"}).status_code == 204
    body = client.get("/api/stats").text
    assert "9.9.9.9" not in body
    assert "device-a" not in body
    assert "device" not in body


# --- rate limit ---


def test_event_rate_limit_is_per_device(
        client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "EVENT_MINUTE_LIMIT", 2)
    for _ in range(2):
        assert _post(client, {"name": "page_view"}).status_code == 204
    blocked = _post(client, {"name": "page_view"})
    assert blocked.status_code == 429
    assert blocked.headers["Retry-After"] == "60"
    # Another device behind the same IP keeps its own bucket.
    assert _post(client, {"name": "page_view"}, DEVICE_B).status_code == 204
