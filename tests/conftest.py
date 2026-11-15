from pathlib import Path

import pytest

from ai_builder.config import load_config
from ai_builder.store import Store


@pytest.fixture
def config():
    return load_config(Path(__file__).parents[1] / "config" / "baseline.json")


@pytest.fixture
def ledger(tmp_path, config):
    store = Store(tmp_path / "db.sqlite3")
    study = store.create_study("US", "30")
    run = store.create_run(study, config)
    price = store.register_pricing({"currency": "USD", "version": "fixture"})
    return store, study, run, price


def reserve(store, run, price, request="req1", upper="1"):
    return store.reserve(run_id=run, request_id=request, logical_request_id="logical-" + request,
                         provider="openai", model="gpt-6-astra", endpoint="responses", agent="critic",
                         purpose="eval", pricing_id=price, upper=upper)
