from decimal import Decimal as D

import pytest
from pydantic import ValidationError

from ai_builder.domain import Criterion, Eligibility, Factor, Interval, eligibility, feasibility, rank_key, score


def result(state="completed", value="pass", evidence=None):
    return Criterion(criterion_id="F01", state=state, result=value,
                     evidence_ids=["e1"] if evidence is None else evidence, reason="Fixture")


@pytest.mark.parametrize("state", ["pending", "running", "technical_failed"])
def test_technical_state_never_business_unknown(state):
    assert eligibility([result(state, None)], ["F01"]) == Eligibility.INCOMPLETE


def test_required_criterion_cannot_be_silently_omitted():
    assert eligibility([result()], ["F01", "F02"]) == Eligibility.INCOMPLETE


@pytest.mark.parametrize("value", ["pass", "fail"])
def test_pass_and_fail_require_evidence(value):
    with pytest.raises(ValidationError):
        result(value=value, evidence=[])


def test_unknown_is_not_failure_or_zero():
    assert eligibility([result(value="unknown", evidence=[])], ["F01"]) == Eligibility.NEEDS_REVIEW


def test_config_rejects_bad_weights_and_missing_filter(config):
    data = config.model_dump()
    data["factors"][0]["weight"] = D(26)
    with pytest.raises(ValidationError):
        type(config).model_validate(data)
    data = config.model_dump()
    data["criteria"].pop()
    with pytest.raises(ValidationError):
        type(config).model_validate(data)


def assessment(config, numbers, days):
    return score({f.id: Factor(low=v, high=v) for f, v in zip(config.factors, numbers)},
                 {f.id: f.weight for f in config.factors}, days=Interval(low=days, high=days),
                 cost=Interval(low=100, high=100), budget=D(15000), resources="available", checks_complete=True)


def test_92_day_model_cannot_outrank_feasible_model(config):
    a = assessment(config, [4, 4, 1, 2, 4], 92)
    b = assessment(config, [3, 3, 3, 3, 2], 60)
    assert a.value == 75 and b.value == D("72.5")
    assert rank_key(("a", "A", a)) < rank_key(("b", "B", b))
    assert a.goal_feasibility == "infeasible" and b.goal_feasibility == "feasible"


def test_honest_interval_is_final_and_conservative(config):
    factors = {f.id: Factor(low=2, high=3) for f in config.factors}
    s = score(factors, {f.id: f.weight for f in config.factors}, days=Interval(low=14, high=15),
              cost=Interval(low=100, high=200), budget=D(15000), resources="available", checks_complete=True)
    assert s.status == "final" and s.value == 50 and s.bounds.high == 75


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1"])
def test_invalid_numeric_estimates(value):
    with pytest.raises(ValidationError):
        Interval(low=value, high=value)


def test_unknown_resources_are_not_available():
    assert feasibility(Interval(low=1, high=2), Interval(low=1, high=2), D(10), "unknown") == "uncertain"
