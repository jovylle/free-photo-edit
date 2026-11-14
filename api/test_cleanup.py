"""TTL checks use only temporary files and ledgers; no provider calls."""
import os
from pathlib import Path
import sqlite3

import pytest

from free_photo_edit_api.credits import CreditsStore


@pytest.mark.parametrize("status", ["done", "failed"])
def test_sweep_boundary_repeat_and_accounting(tmp_path, status):
    from free_photo_edit_api import cleanup
    store = CreditsStore(str(tmp_path / "ledger.db"))
    store.check_and_debit("u", "job", ip="ip")
    if status == "done":
        store.complete("job")
    else:
        store.refund("u", "job", "boom")
    job = store.get_job("job")
    balance = store.balance("u")
    folder = tmp_path / "storage" / "job"
    folder.mkdir(parents=True)
    for name in ("input_0.png", "output.png"):
        (folder / name).write_bytes(b"photo")
    deadline = job["terminal_at"] + cleanup.RETENTION_SECONDS
    cleanup.sweep(store, folder.parent, now=deadline - 0.01)
    assert (folder / "output.png").exists()
    cleanup.sweep(store, folder.parent, now=deadline)
    cleanup.sweep(store, folder.parent, now=deadline + 1)
    assert not folder.exists()
    assert store.get_job("job") == job
    assert store.balance("u") == balance
    assert store.ip_job_count("ip") == 1
    assert store.count_jobs_since(0) == 1


def test_sweep_preserves_running_unknown_and_symlinks(tmp_path):
    from free_photo_edit_api import cleanup
    store = CreditsStore(str(tmp_path / "ledger.db"))
    root = tmp_path / "storage"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "output.png").write_bytes(b"safe")
    for name in ("running", "queued", "unknown", "linked", "mixed", "../outside"):
        if name != "unknown":
            store.check_and_debit(name, name)
            if name != "running":
                store.complete(name, "queued" if name == "queued" else "done")
        if name == "../outside":
            continue
        if name == "linked":
            (root / name).symlink_to(outside, target_is_directory=True)
        else:
            (root / name).mkdir()
            (root / name / "output.png").write_bytes(b"safe")
    (root / "mixed" / "input_0.png").symlink_to(outside / "output.png")
    (root / "mixed" / "notes.txt").write_text("keep")
    (root / "mixed" / "input_0.jpg").mkdir()
    cleanup.sweep(store, root, now=10**12)
    assert (root / "running" / "output.png").exists()
    assert (root / "unknown" / "output.png").exists()
    assert (root / "queued" / "output.png").exists()
    assert (root / "mixed" / "input_0.jpg").is_dir()
    assert (root / "linked").is_symlink()
    assert (root / "mixed" / "input_0.png").is_symlink()
    assert (root / "mixed" / "notes.txt").exists()
    assert (outside / "output.png").read_bytes() == b"safe"
    link = tmp_path / "root-link"
    link.symlink_to(outside, target_is_directory=True)
    cleanup.sweep(store, link, now=10**12)
    assert (outside / "output.png").exists()

def test_legacy_terminal_migration_grants_grace_once(tmp_path, monkeypatch):
    from free_photo_edit_api import credits, cleanup
    path = str(tmp_path / "legacy.db")
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE jobs(id TEXT PRIMARY KEY, user TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL)")
    for status in ("done", "failed", "running"):
        db.execute("INSERT INTO jobs VALUES(?,?,?,?)", (status, "u", status, 1.0))
    db.commit()
    db.close()
    monkeypatch.setattr(credits.time, "time", lambda: 1000000.0)
    store = CreditsStore(path)
    assert store.get_job("running")["terminal_at"] is None
    for status in ("done", "failed"):
        assert store.get_job(status)["terminal_at"] == 1000000.0
        assert not cleanup.artifacts_expired(store.get_job(status))
    monkeypatch.setattr(credits.time, "time", lambda: 1000100.0)
    reopened = CreditsStore(path)
    assert reopened.get_job("done")["terminal_at"] == 1000000.0
    assert reopened.get_job("done")["created_at"] == 1.0


def test_failed_unlink_retries_and_other_jobs_continue(tmp_path, monkeypatch):
    from free_photo_edit_api import cleanup
    store = CreditsStore(str(tmp_path / "ledger.db"))
    root = tmp_path / "storage"
    for job_id in ("first", "second"):
        store.check_and_debit(job_id, job_id)
        store.complete(job_id)
        (root / job_id).mkdir(parents=True)
        (root / job_id / "output.png").write_bytes(b"photo")
    unlink = os.unlink
    calls = []
    def denied_once(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise PermissionError("simulated")
        return unlink(*args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(cleanup.os, "unlink", denied_once)
        cleanup.sweep(store, root, now=10**12)
    assert sum((root / name / "output.png").exists() for name in ("first", "second")) == 1
    cleanup.sweep(store, root, now=10**12)
    assert list(root.iterdir()) == []


def test_real_startup_sweep_preserves_recent_and_running(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from free_photo_edit_api import server, cleanup
    root = tmp_path / "storage"
    monkeypatch.setattr(server, "STORAGE", root)
    store = server.reset_store_for_tests(str(tmp_path / "ledger.db"))
    for job_id in ("expired", "recent", "running"):
        store.check_and_debit(job_id, job_id)
        if job_id != "running":
            store.complete(job_id)
        (root / job_id).mkdir(parents=True)
        (root / job_id / "output.png").write_bytes(b"photo")
    deadline = store.get_job("expired")["terminal_at"] + cleanup.RETENTION_SECONDS
    # Only the old terminal job expires; creation/mtime never determine retention.
    store._db.execute("UPDATE jobs SET terminal_at=? WHERE id='recent'", (deadline,))
    store._db.commit()
    monkeypatch.setattr(cleanup.time, "time", lambda: deadline)
    with TestClient(server.app) as client:
        assert not (root / "expired").exists()
        for job_id in ("recent", "running"):
            assert (root / job_id / "output.png").exists()
            assert client.get(f"/api/result/{job_id}").json()["artifacts_expired"] is False
    assert store.get_job("running")["status"] == "running"

@pytest.mark.parametrize("status", ["done", "failed"])
def test_expired_api_consistent_before_and_after_sweep(tmp_path, monkeypatch, status):
    from fastapi.testclient import TestClient
    from free_photo_edit_api import server, cleanup
    monkeypatch.setattr(server, "STORAGE", tmp_path / "storage")
    store = server.reset_store_for_tests(str(tmp_path / "ledger.db"))
    store.check_and_debit("u", "job")
    store.complete("job", status)
    folder = server.STORAGE / "job"
    folder.mkdir(parents=True)
    for name in ("input_0.png", "output.png"):
        (folder / name).write_bytes(b"photo")
    now = store.get_job("job")["terminal_at"] + cleanup.RETENTION_SECONDS
    monkeypatch.setattr(cleanup.time, "time", lambda: now)
    client = TestClient(server.app)
    for swept in (False, True):
        if swept:
            cleanup.sweep(store, server.STORAGE)
        response = client.get("/api/result/job?user=u")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == status
        assert body.get("artifacts_expired") is True
        assert body["image_url"] is None
        history = client.get("/api/jobs?user=u").json()["jobs"][0]
        assert history["artifacts_expired"] is True
        assert history["inputs"] == []
        assert history["image_url"] is None
        for suffix in ("file", "input/0"):
            assert client.get(f"/api/result/job/{suffix}?user=u").status_code == 410
    assert client.get("/api/result/unknown/file").status_code == 404


def test_automatic_sweep_startup_periodic_and_shutdown(tmp_path, monkeypatch):
    import threading
    from fastapi.testclient import TestClient
    from free_photo_edit_api import server, cleanup
    monkeypatch.setattr(server, "STORAGE", tmp_path / "storage")
    server.reset_store_for_tests(str(tmp_path / "ledger.db"))
    called = threading.Event()
    calls = []
    def sweep(*args):
        calls.append(threading.current_thread())
        if len(calls) >= 2:
            called.set()
    monkeypatch.setattr(server, "sweep", sweep, raising=False)
    monkeypatch.setattr(server, "SWEEP_INTERVAL_SECONDS", 0.01, raising=False)
    with TestClient(server.app):
        assert called.wait(1), "startup and periodic cleanup never ran"
    assert len(calls) >= 2
    assert not calls[-1].is_alive()



@pytest.mark.parametrize("transition", ["complete", "refund", "fail"])
def test_terminal_clock_is_stable_and_running_has_no_expiry(tmp_path, monkeypatch, transition):
    from free_photo_edit_api import credits
    store = CreditsStore(str(tmp_path / "ledger.db"))
    monkeypatch.setattr(credits.time, "time", lambda: 100.0)
    store.check_and_debit("u", "done")
    assert store.get_job("done").get("terminal_at") is None
    store.complete("done")
    assert store.get_job("done").get("terminal_at") == 100.0
    monkeypatch.setattr(credits.time, "time", lambda: 200.0)
    store.complete("done")
    assert store.get_job("done")["terminal_at"] == 100.0
    store.check_and_debit("u", "failed")
    if transition == "refund":
        store.refund("u", "failed", "boom")
    elif transition == "fail":
        store.fail("failed", "boom")
    else:
        store.complete("failed")
    assert store.get_job("failed")["terminal_at"] == 200.0
    assert store.user_jobs("u")[0]["terminal_at"] == 200.0
