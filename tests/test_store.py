from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import sqlite3

import pytest

from ai_builder.store import BudgetExceeded, Conflict, Store
from conftest import reserve


def test_parallel_requests_cannot_overspend_run(ledger):
    store, _, run, price = ledger
    barrier = Barrier(2)
    def request(i):
        barrier.wait()
        try:
            reserve(store, run, price, str(i), "15")
            return True
        except (BudgetExceeded, Conflict):
            return False
    with ThreadPoolExecutor(2) as pool:
        assert sum(pool.map(request, [1, 2])) == 1
    assert store.summary(run)["reserved_micro"] == 15_000_000
    with store.connection() as db:
        assert db.execute("SELECT status FROM runs WHERE id=?", (run,)).fetchone()[0] == "paused"


def test_study_limit_spans_runs(ledger, config):
    store, study, run, price = ledger
    other = store.create_run(study, config)
    reserve(store, run, price, upper="20")
    with pytest.raises(BudgetExceeded):
        reserve(store, other, price, "req2", "20")


def test_timeout_is_durable_and_late_usage_settles_once(ledger):
    store, _, run, price = ledger
    reserve(store, run, price, upper="12")
    store.mark_unknown("req1")
    reopened = Store(store.path)
    assert reopened.summary(run)["unknown_micro"] == 12_000_000
    assert reopened.summary(run)["remaining_micro"] == 13_000_000
    reopened.settle("req1", "2.5", {"input_tokens": 100})
    reopened.settle("req1", "2.5", {"input_tokens": 100})
    result = reopened.summary(run)
    assert result["calculated_micro"] == 2_500_000 and result["unknown_micro"] == 0
    assert result["remaining_micro"] == 22_500_000
    assert result["billed_unknown_count"] == 1


def test_idempotent_reservation_does_not_allow_resend(ledger):
    store, _, run, price = ledger
    assert reserve(store, run, price)
    assert reserve(store, run, price) is False
    with pytest.raises(Conflict):
        reserve(store, run, price, upper="2")


def test_unknown_usage_never_becomes_free_on_restart(ledger):
    store, _, run, price = ledger
    reserve(store, run, price, upper="25")
    store.mark_unknown("req1")
    with pytest.raises(BudgetExceeded):
        reserve(Store(store.path), run, price, "retry", "0.01")


def test_actual_above_reserved_pauses_new_calls(ledger):
    store, _, run, price = ledger
    reserve(store, run, price)
    store.settle("req1", "2", {})
    with pytest.raises(Conflict):
        reserve(store, run, price, "req2")


def test_free_provider_credit_does_not_hide_consumption(ledger):
    store, _, run, price = ledger
    reserve(store, run, price)
    store.settle("req1", "0.008", {"credits": 1}, billed="0")
    assert store.summary(run)["calculated_micro"] == 8000
    assert store.summary(run)["billed_micro"] == 0
    assert store.summary(run)["billed_unknown_count"] == 0


def test_correction_replaces_cost_instead_of_adding_it(ledger):
    store, _, run, price = ledger
    reserve(store, run, price, upper="3")
    store.settle("req1", "1", {})
    store.settle("req1", "2", {})
    assert store.summary(run)["calculated_micro"] == 2_000_000
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM events WHERE kind='usage_corrected'").fetchone()[0] == 1


def test_secret_bearing_usage_rejected(ledger):
    store, _, run, price = ledger
    reserve(store, run, price)
    with pytest.raises(ValueError):
        store.settle("req1", "1", {"Authorization": "secret"})


def test_old_worker_cannot_overwrite_recovered_stage(ledger):
    store, _, run, _ = ledger
    identifier = store.enqueue(run, "scope", "research", "hash")
    assert store.enqueue(run, "scope", "research", "hash") == identifier
    first = store.claim(10, now=100)
    assert store.claim(10, now=105) is None
    second = store.claim(10, now=111)
    with pytest.raises(Conflict):
        store.finish(identifier, first["token"], {"wrong": True}, now=112)
    store.finish(identifier, second["token"], {"valid": True}, ("critic", "next-hash"), now=112)
    assert store.claim(now=113)["name"] == "critic"


def test_failed_next_stage_insert_rolls_back_result(ledger):
    store, _, run, _ = ledger
    identifier = store.enqueue(run, "scope", "research", "hash")
    stage = store.claim(now=100)
    with pytest.raises(sqlite3.IntegrityError):
        store.finish(identifier, stage["token"], {"valid": True}, (None, "next"), now=101)
    with store.connection() as db:
        assert db.execute("SELECT status FROM stages WHERE id=?", (identifier,)).fetchone()[0] == "running"
