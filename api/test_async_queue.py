"""Real background jobs with event-gated mocked engines; no live AI calls."""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from free_photo_edit_api import server
from free_photo_edit_api.engines import EngineError

PNG = b"\x89PNG\r\n\x1a\nmock output"


class FakeEngine:
    def __init__(self):
        self.release = threading.Event()
        self.entered = threading.Condition()
        self.calls = 0
        self.fail = False

    def edit(self, input_bytes, prompt):
        with self.entered:
            self.calls += 1
            self.entered.notify_all()
        if not self.release.wait(10):
            raise AssertionError("test did not release engine")
        if self.fail:
            raise EngineError("engine boom")
        return PNG

    def wait_calls(self, count):
        with self.entered:
            assert self.entered.wait_for(lambda: self.calls >= count, timeout=5)


@pytest.fixture()
def engine():
    return FakeEngine()


@pytest.fixture()
def client(tmp_path, monkeypatch, engine):
    monkeypatch.setattr(server, "STORAGE", tmp_path / "storage")
    server.reset_store_for_tests(str(tmp_path / "credits.db"))
    server.reset_slots_for_tests()
    monkeypatch.setattr(server, "select_engine", lambda: engine)
    monkeypatch.setattr(server, "TURNSTILE_SECRET", "")
    # Measure slot limits, not the independent request-rate gates.
    monkeypatch.setattr(server, "IP_MINUTE_LIMIT", 100)
    monkeypatch.setattr(server, "IP_DAILY_LIMIT", 100)
    threads = []
    worker = server._run_job_thread

    def tracked_worker(*args):
        threads.append(threading.current_thread())
        worker(*args)

    monkeypatch.setattr(server, "_run_job_thread", tracked_worker)
    with TestClient(server.app) as c:
        try:
            yield c
        finally:
            engine.release.set()
            for thread in threads:
                thread.join(timeout=5)
                assert not thread.is_alive()


def _edit(client, user, ip="192.0.2.1"):
    return client.post(
        "/api/edit", headers={"CF-Connecting-IP": ip},
        data={"prompt": "fix it", "preset": "restore", "user": user},
        files={"image": ("in.png", PNG, "image/png")},
    )


def _wait_status(client, job_id, want="done"):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        r = client.get(f"/api/result/{job_id}")
        assert r.status_code == 200, r.text
        if r.json()["status"] == want:
            return r.json()
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} never {want}")


def test_running_then_done(client, engine):
    r = _edit(client, "run")
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    engine.wait_calls(1)
    running = client.get(f"/api/result/{job_id}")
    assert running.status_code == 200
    assert running.json()["status"] == "running"
    assert running.json()["image_url"] is None
    assert client.get(f"/api/result/{job_id}/file?user=run").status_code == 404
    assert server.get_store().balance("run") == 4
    engine.release.set()
    _wait_status(client, job_id)
    output = client.get(f"/api/result/{job_id}/file?user=run")
    assert output.status_code == 200
    assert output.content == PNG
    assert (server.STORAGE / job_id / "output.png").read_bytes() == PNG


@pytest.mark.parametrize("same_user", [False, True])
def test_three_slots_fourth_429_unbilled(client, engine, same_user):
    store = server.get_store()
    store.set_credits("shared", 10)
    users = ["shared"] * 3 if same_user else [f"user{i}" for i in range(3)]
    accepted = [_edit(client, user) for user in users]
    assert [r.status_code for r in accepted] == [202] * 3
    engine.wait_calls(3)
    before = store.balance("shared")
    overflow = _edit(client, "shared")
    assert overflow.status_code == 429, overflow.text
    assert overflow.headers["Retry-After"] == "10"
    assert store.balance("shared") == before
    assert engine.calls == 3
    assert len(store.user_jobs("shared")) == (3 if same_user else 0)
    # A different IP has independent capacity, even with the same user token.
    other = _edit(client, "shared", "192.0.2.2")
    assert other.status_code == 202, other.text
    engine.wait_calls(4)
    engine.release.set()
    for r in accepted + [other]:
        _wait_status(client, r.json()["job_id"])


def test_global_slots_full_429_and_reuse(client, engine):
    accepted = [_edit(client, f"global{i}", f"192.0.2.{i + 1}") for i in range(6)]
    assert [r.status_code for r in accepted] == [202] * 6
    engine.wait_calls(6)
    r = _edit(client, "overflow", "192.0.2.99")
    assert r.status_code == 429, r.text
    assert r.headers["Retry-After"] == "15"
    assert server.get_store().balance("overflow") == 5
    assert server.get_store().user_jobs("overflow") == []
    assert engine.calls == 6
    engine.release.set()
    for r in accepted:
        _wait_status(client, r.json()["job_id"])
    reused = _edit(client, "overflow", "192.0.2.99")
    assert reused.status_code == 202, reused.text
    _wait_status(client, reused.json()["job_id"])


def test_engine_failure_refunds_and_reuses_all_slots(client, engine):
    engine.fail = True
    accepted = [_edit(client, f"fail{i}", f"192.0.2.{i // 3 + 1}") for i in range(6)]
    assert [r.status_code for r in accepted] == [202] * 6
    engine.wait_calls(6)
    engine.release.set()
    for i, r in enumerate(accepted):
        job_id = r.json()["job_id"]
        failed = _wait_status(client, job_id, "failed")
        assert failed["image_url"] is None
        assert server.get_store().balance(f"fail{i}") == 5
        assert "boom" in server.get_store().get_job(job_id)["error"]
        assert client.get(f"/api/result/{job_id}/file?user=fail{i}").status_code == 404
    engine.fail = False
    engine.release.clear()
    reused = [_edit(client, f"retry{i}", f"192.0.2.{i // 3 + 1}") for i in range(6)]
    assert [r.status_code for r in reused] == [202] * 6
    engine.wait_calls(12)
    engine.release.set()
    for r in reused:
        _wait_status(client, r.json()["job_id"])


def test_concurrent_double_submit_ok(client, engine):
    results = []
    barrier = threading.Barrier(2)

    def go():
        with TestClient(server.app) as c:
            barrier.wait(timeout=5)
            results.append(_edit(c, "race2"))

    ts = [threading.Thread(target=go) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=5)
        assert not t.is_alive()
    assert sorted(r.status_code for r in results) == [202, 202]
    engine.wait_calls(2)
    assert server.get_store().balance("race2") == 3
    engine.release.set()
    for r in results:
        _wait_status(client, r.json()["job_id"])
