"""FPE-3 contract tests: 202 + poll-until-done, credit decrement,
validation rejections. Mocked engine — no live AI calls."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from free_photo_edit_api import server

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
        assert input_bytes.startswith(b"\x89PNG")
        assert len(prompt) > 0
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
    monkeypatch.setattr(server, "TURNSTILE_SECRET", "")  # hermetic: real .env must not gate tests
    FakeEngine.calls = 0
    return TestClient(server.app)


def _file(payload=PNG, ctype="image/png"):
    return {"image": ("in.png", payload, ctype)}


def _edit(client, user, **kw):
    data = {"prompt": "make the sky sunset", "preset": "custom", "user": user}
    data.update(kw.get("data", {}))
    return client.post("/api/edit", headers={"X-User": user}, data=data,
                       files=kw.get("files", _file()))


def _wait_done(client, job_id, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/result/{job_id}")
        assert r.status_code == 200, r.text
        if r.json()["status"] == "done":
            return r.json()
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never done")


def test_edit_202_and_poll_until_done(client):
    store = server.get_store()
    assert store.balance("ann") == 3
    r = _edit(client, "ann")
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    assert r.json()["credits_left"] == 2
    body = _wait_done(client, job_id)
    assert body["image_url"] == f"/api/result/{job_id}/file"
    f = client.get(f"/api/result/{job_id}/file")
    assert f.status_code == 200
    assert f.content.startswith(b"\x89PNG")
    assert FakeEngine.calls == 1


def test_zero_credit_402_zero_engine_calls(client):
    store = server.get_store()
    store.set_credits("broke", 0)
    r = _edit(client, "broke")
    assert r.status_code == 402, r.text
    assert FakeEngine.calls == 0


def test_bad_mime_rejected(client):
    r = _edit(client, "gif", files={"image": ("in.gif", PNG, "image/gif")})
    assert r.status_code == 400, r.text
    assert FakeEngine.calls == 0


def test_oversize_rejected(client):
    big = PNG + b"\x00" * (5 * 1024 * 1024)
    r = _edit(client, "big", files={"image": ("in.png", big, "image/png")})
    assert r.status_code == 400, r.text
    assert FakeEngine.calls == 0


def test_unknown_preset_rejected(client):
    r = _edit(client, "u1", data={"prompt": "x", "preset": "nope", "user": "u1"})
    assert r.status_code == 400, r.text
    assert FakeEngine.calls == 0


def test_empty_prompt_rejected(client):
    r = _edit(client, "u2", data={"prompt": "", "preset": "custom", "user": "u2"})
    assert r.status_code == 400, r.text
    assert FakeEngine.calls == 0


def test_credits_endpoint(client):
    r = client.get("/api/credits", params={"user": "ann"})
    assert r.status_code == 200
    assert r.json()["credits_left"] == 3


def test_jobs_history(client):
    r = _edit(client, "hist")
    assert r.status_code == 202
    _wait_done(client, r.json()["job_id"])
    h = client.get("/api/jobs", params={"user": "hist"})
    assert h.status_code == 200
    jobs = h.json()["jobs"]
    assert len(jobs) >= 1
    assert jobs[0]["status"] == "done"
