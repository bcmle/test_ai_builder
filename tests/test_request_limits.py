import pytest

from ai_builder.store import BudgetExceeded
from conftest import reserve


def test_released_attempts_still_count_towards_request_limit(ledger, config):
    store, study, _, price = ledger
    data = config.model_dump()
    data["limits"]["provider_requests"] = 1
    run = store.create_run(study, type(config).model_validate(data))
    reserve(store, run, price, "one")
    store.release("one", "Test transport confirmed no request sent")
    with pytest.raises(BudgetExceeded):
        reserve(store, run, price, "two")


@pytest.mark.parametrize("metrics", [
    {"input_tokens": -1}, {"output_tokens": 1.5}, {"input_tokens": True},
    {"input_tokens": 10, "cached_input_tokens": 11},
    {"output_tokens": 10, "reasoning_tokens": 11},
])
def test_invalid_usage_rejected(ledger, metrics):
    store, _, run, price = ledger
    reserve(store, run, price)
    with pytest.raises(ValueError):
        store.settle("req1", "1", metrics)
