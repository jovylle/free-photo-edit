"""FPE-3 contract tests: 202 + poll-until-done, credit decrement,
validation rejections. Mocked engine — no live AI calls."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx
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


def _file(payload: bytes = PNG, ctype: str = "image/png") -> dict[str, tuple[str, bytes, str]]:
    return {"image": ("in.png", payload, ctype)}


def _edit(client: TestClient, user: str, **kw: Any) -> httpx.Response:
    data = {"prompt": "make the sky sunset", "preset": "custom", "user": user}
    data.update(kw.get("data", {}))
    return client.post("/api/edit", headers={"X-User": user}, data=data,
                       files=kw.get("files", _file()))


def _wait_done(client: TestClient, job_id: str, timeout: float = 10.0) -> dict[str, Any]:
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
    assert store.balance("ann") == 5
    r = _edit(client, "ann")
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    assert r.json()["credits_left"] == 4
    body = _wait_done(client, job_id)
    assert body["image_url"] == f"/api/result/{job_id}/file"
    # The stage card shows inputs above the result, so the single-job payload
    # must carry them just like /api/jobs does.
    assert body["inputs"] == [f"/api/result/{job_id}/input/0"]
    f = client.get(f"/api/result/{job_id}/file?user=ann")
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


@pytest.mark.parametrize("extra_field", ["other"])
def test_extra_upload_rejected_without_debit(client, extra_field):
    r = _edit(client, "extra", files=[
        ("image", ("in.png", PNG, "image/png")),
        (extra_field, ("extra.png", PNG, "image/png")),
    ])
    assert r.status_code == 400, r.text
    assert FakeEngine.calls == 0
    assert server.get_store().balance("extra") == 5
    assert server.get_store().user_jobs("extra") == []


def test_multiple_photos_accepted(client):
    r = _edit(client, "multi", files=[
        ("image", ("a.png", PNG, "image/png")),
        ("image", ("b.png", PNG, "image/png")),
    ])
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    for index in (0, 1):
        assert client.get(f"/api/result/{job_id}/input/{index}?user=multi").status_code == 200
    assert client.get(f"/api/result/{job_id}/input/2?user=multi").status_code == 404


@pytest.mark.parametrize("failure", ["mkdir", "write_bytes", "thread_start"])
def test_setup_failure_refunds_and_releases_slots(client, monkeypatch, failure):
    def boom(*args, **kwargs):
        raise OSError("setup boom")

    with monkeypatch.context() as patch:
        if failure == "thread_start":
            # Do not patch Thread.start globally: TestClient also needs threads.
            class BrokenThread:
                def __init__(self, *args, **kwargs):
                    pass

                start = boom

            from types import SimpleNamespace
            patch.setattr(server, "threading", SimpleNamespace(
                Thread=BrokenThread, Semaphore=server.threading.Semaphore))
        else:
            patch.setattr(Path, failure, boom)
        r = _edit(client, "setup")
    assert r.status_code == 502, r.text
    assert FakeEngine.calls == 0
    store = server.get_store()
    assert store.balance("setup") == 5
    job = store.user_jobs("setup")[0]
    assert job["status"] == "failed"
    assert "setup boom" in store.get_job(job["id"])["error"]
    poll = client.get(f"/api/result/{job['id']}")
    assert poll.status_code == 200
    assert poll.json()["status"] == "failed"
    for sem, count in [(server._slots_for("testclient"), 3), (server._global_slots, 6)]:
        for _ in range(count):
            assert sem.acquire(blocking=False)
        assert not sem.acquire(blocking=False)
        for _ in range(count):
            sem.release()
    retry = _edit(client, "setup")
    assert retry.status_code == 202, retry.text
    _wait_done(client, retry.json()["job_id"])


@pytest.mark.parametrize("files", [{}, {"other": ("in.png", PNG, "image/png")},
                                  {"image": ("in.png", b"", "image/png")}])
def test_missing_or_empty_image_rejected(client, files):
    r = _edit(client, "missing", files=files)
    assert r.status_code == 400, r.text
    assert FakeEngine.calls == 0
    assert server.get_store().balance("missing") == 5


@pytest.mark.parametrize("ctype", ["image/jpeg", "image/png", "image/webp"])
def test_allowed_mime_at_size_limit(client, ctype):
    payload = PNG + b"\x00" * (server.MAX_FILE_BYTES - len(PNG))
    r = _edit(client, "boundary", files=_file(payload, ctype))
    assert r.status_code == 202, r.text
    _wait_done(client, r.json()["job_id"])
    assert server.get_store().balance("boundary") == 4


def test_credits_endpoint(client):
    r = client.get("/api/credits", params={"user": "ann"})
    assert r.status_code == 200
    assert r.json()["credits_left"] == 5


def test_config_reports_revision(client: TestClient) -> None:
    body = client.get("/api/config").json()
    assert isinstance(body["revision"], str) and body["revision"]


def test_metrics_exposes_counts_without_identity(client: TestClient) -> None:
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "photoedit_up 1" in r.text
    assert "photoedit_users_total" in r.text
    assert "photoedit_revision_info{revision=" in r.text
    assert "ann" not in r.text  # no per-user data


def test_jobs_history(client):
    r = _edit(client, "hist")
    assert r.status_code == 202
    _wait_done(client, r.json()["job_id"])
    h = client.get("/api/jobs", params={"user": "hist"})
    assert h.status_code == 200
    jobs = h.json()["jobs"]
    assert len(jobs) >= 1
    assert jobs[0]["status"] == "done"


def test_html_revalidates_but_api_does_not(client):
    assert client.get("/").headers.get("cache-control") == "no-cache"
    assert client.get("/index.html").headers.get("cache-control") == "no-cache"
    assert "cache-control" not in client.get("/api/config").headers


def test_cors_allows_only_configured_origin(client, monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", "https://free-photo-edit-dev.pages.dev")
    ok = client.get("/api/config", headers={"Origin": "https://free-photo-edit-dev.pages.dev"})
    assert ok.headers.get("access-control-allow-origin") == "https://free-photo-edit-dev.pages.dev"
    bad = client.get("/api/config", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in bad.headers
    pre = client.options("/api/edit", headers={
        "Origin": "https://free-photo-edit-dev.pages.dev",
        "Access-Control-Request-Method": "POST"})
    assert pre.status_code == 204
    assert pre.headers.get("access-control-allow-origin") == "https://free-photo-edit-dev.pages.dev"


def test_result_file_is_owner_only(client):
    """A leaked job id must not be enough to read someone's edited photo."""
    r = _edit(client, "owner")
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    _wait_done(client, job_id)
    assert client.get(f"/api/result/{job_id}/file?user=owner").status_code == 200
    assert client.get(f"/api/result/{job_id}/file?user=someone-else").status_code == 404
    assert client.get(f"/api/result/{job_id}/file").status_code == 404
    assert client.get(f"/api/result/{job_id}/input/0?user=someone-else").status_code == 404


