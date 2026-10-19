"""FPE-3 async queue tests: running→done, 3-per-user 429, 6-global 429,
engine failure → refund + failed. Mocked engines only."""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from free_photo_edit_api import server
from free_photo_edit_api.engines import EngineError

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


class BoomEngine:
    name = "boom"

    def edit(self, input_bytes, prompt):
        raise EngineError("engine boom")


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
    monkeypatch.setattr(server, "TURNSTILE_SECRET", "")  # hermetic: real .env must not gate tests
    FakeEngine.calls = 0
    return TestClient(server.app)


def _edit(client, user):
    return client.post(
        "/api/edit", headers={"X-User": user},
        data={"prompt": "fix it", "preset": "restore", "user": user},
        files={"image": ("in.png", PNG, "image/png")},
    )


def _wait_status(client, job_id, want="done", timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = client.get(f"/api/result/{job_id}").json()["status"]
        if s == want:
            return
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never {want}")


def test_running_then_done(client):
    r = _edit(client, "run")
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    _wait_status(client, job_id, "done")
    assert client.get(f"/api/result/{job_id}/file").status_code == 200


def test_three_slots_fourth_429_unbilled(client):
    store = server.get_store()
    sem = server._slots_for("slotty")
    assert sem.acquire(blocking=False)
    assert sem.acquire(blocking=False)
    assert sem.acquire(blocking=False)
    try:
        r = _edit(client, "slotty")
        assert r.status_code == 429, r.text
        assert r.headers.get("Retry-After") == "10"
    finally:
        sem.release()
        sem.release()
        sem.release()
    assert store.balance("slotty") == 3
    assert FakeEngine.calls == 0


def test_global_slots_full_429(client):
    held = []
    for _ in range(server.MAX_GLOBAL):
        assert server._global_slots.acquire(blocking=False)
        held.append(True)
    try:
        r = _edit(client, "glob")
        assert r.status_code == 429, r.text
    finally:
        for _ in held:
            server._global_slots.release()
    assert server.get_store().balance("glob") == 3


def test_engine_failure_refunds_and_marks_failed(client, monkeypatch):
    monkeypatch.setattr(server, "select_engine", lambda: BoomEngine())
    store = server.get_store()
    assert store.balance("unlucky") == 3
    r = _edit(client, "unlucky")
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    _wait_status(client, job_id, "failed")
    assert store.balance("unlucky") == 3
    assert "boom" in (store.get_job(job_id)["error"] or "")


def test_concurrent_double_submit_ok(client):
    results = []
    barrier = threading.Barrier(2)

    def go():
        c = TestClient(server.app)
        barrier.wait()
        results.append(_edit(c, "race2"))

    ts = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(r.status_code for r in results) == [202, 202]
