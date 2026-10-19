"""FPE-1 ledger tests: FREE_CREDITS=3, atomic check-then-debit,
0-credit raises without touching ledger, concurrent double-submit,
3-cap, refund on engine failure, IP job count."""

from __future__ import annotations

import threading

import pytest

from free_photo_edit_api.credits import FREE_CREDITS, CreditsStore, OutOfCredits


@pytest.fixture()
def store(tmp_path):
    return CreditsStore(str(tmp_path / "credits.db"))


def test_new_user_gets_three_credits(store):
    assert FREE_CREDITS == 3
    assert store.balance("alice") == 3


def test_check_and_debit_decrements_and_records_job(store):
    left = store.check_and_debit("bob", "job-1", preset="background", ip="1.1.1.1")
    assert left == 2
    assert store.balance("bob") == 2
    job = store.get_job("job-1")
    assert job is not None
    assert job["status"] == "running"
    assert job["preset"] == "background"


def test_zero_credit_raises_without_ledger_touch(store):
    store.set_credits("broke", 0)
    with pytest.raises(OutOfCredits):
        store.check_and_debit("broke", "job-x", ip="2.2.2.2")
    assert store.balance("broke") == 0
    assert store.get_job("job-x") is None


def test_sequential_double_submit_debits_twice(store):
    assert store.balance("dubs") == 3
    store.check_and_debit("dubs", "j1", ip="3.3.3.3")
    store.check_and_debit("dubs", "j2", ip="3.3.3.3")
    assert store.balance("dubs") == 1


def test_three_cap_fourth_raises(store):
    store.check_and_debit("cap", "c1", ip="4.4.4.4")
    store.check_and_debit("cap", "c2", ip="4.4.4.4")
    store.check_and_debit("cap", "c3", ip="4.4.4.4")
    assert store.balance("cap") == 0
    with pytest.raises(OutOfCredits):
        store.check_and_debit("cap", "c4", ip="4.4.4.4")
    assert store.balance("cap") == 0


def test_concurrent_double_submit_both_debited_once(store):
    assert store.balance("race") == 3
    errors: list = []

    def go(job_id: str):
        try:
            store.check_and_debit("race", job_id, ip="5.5.5.5")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=go, args=(f"r{i}",)) for i in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert errors == []
    assert store.balance("race") == 1


def test_refund_restores_credit_and_marks_failed(store):
    store.check_and_debit("fred", "f1", ip="6.6.6.6")
    assert store.balance("fred") == 2
    left = store.refund("fred", "f1", error="engine boom")
    assert left == 3
    assert store.balance("fred") == 3
    assert store.get_job("f1")["status"] == "failed"


def test_ip_job_count_tracks_submissions(store):
    store.check_and_debit("u1", "ip1", ip="9.9.9.9")
    store.check_and_debit("u2", "ip2", ip="9.9.9.9")
    assert store.ip_job_count("9.9.9.9") == 2
    assert store.ip_job_count("8.8.8.8") == 0
