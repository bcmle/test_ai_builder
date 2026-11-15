import pytest

from ai_builder.domain import Claim, Risk, handoff, reconcile


def claim(identifier, **changes):
    return Claim.model_validate({"claim_id": identifier, "neutral_text": "The published price is $10.",
        "kind": "fact", "entity": "Fixture", "geography": "US", "period": None, "metric": "price",
        "source_urls": ["https://example.com"], "evidence_refs": [{"evidence_id": "e1",
            "source_snapshot_id": "s1", "excerpt": "$10", "locator": "pricing"}], "formula": None,
        "input_claim_ids": [], "quantity_estimate": None, "affected_criterion_ids": [], **changes})


def risk(**changes):
    return Risk.model_validate({"risk_id": "r1", "impact": 3, "critic_order": 1,
        "premise_claim_ids": ["c1"], "essential_premise_ids": ["c1"], **changes})


def test_handoff_carries_transitive_inputs_but_not_critic_opinion():
    claims = [claim("c1", kind="calculation", formula="c2 * 2", input_claim_ids=["c2"],
                    confidence='high', confidence_reason='Critic opinion',
                    assumptions=['Critic forecast'], method='calculation'), claim("c2")]
    result = handoff(claims, [risk()], {"s1"}, {"F01"})
    assert {c.claim_id for c in result} == {"c1", "c2"}
    for c in result:
        assert not set(c.model_dump()) & {"impact", "confidence", "confidence_reason", "assumptions", "method", "critic_order", "affected_criterion_ids", "score"}
        assert c.evidence_refs[0].excerpt == "$10"


def test_filter_claim_is_checked_even_if_not_in_selected_risks():
    assert len(handoff([claim("c1", affected_criterion_ids=["F01"])], [], {"s1"}, {"F01"})) == 1


@pytest.mark.parametrize("claims,allowed", [
    ([claim("c1", input_claim_ids=["missing"])], {"s1"}),
    ([claim("c1", input_claim_ids=["c1"])], {"s1"}),
    ([claim("c1")], {"other-scope"}),
])
def test_invalid_handoff_dependency_or_scope_rejected(claims, allowed):
    with pytest.raises(ValueError):
        handoff(claims, [risk()], allowed, {"F01"})


@pytest.mark.parametrize("verification,expected", [
    ({"c1": "refuted"}, "refuted_basis"), ({"c1": "supported"}, "supported_basis"),
    ({"c1": "mixed"}, "unresolved"), ({}, "unresolved"),
])
def test_reconciliation_preserves_uncertainty(verification, expected):
    assert reconcile(risk(), verification) == expected
