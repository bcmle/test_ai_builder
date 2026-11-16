"""A source-free arithmetic check must not certify its forecast inputs as facts."""
from copy import deepcopy

import pytest

from ai_builder.pipeline import (FactorsV153, FactorsStateV153, Verification,
                                materialize_milestones, validate_factor_verification)
from test_milestone_pipeline import milestone_output


def fixture():
    raw = milestone_output({'eligibility_conditions':[]})
    factors = materialize_milestones(FactorsV153.model_validate(raw),'US')
    checked = Verification.model_validate({'items':[
        {'claim_id':claim.claim_id,
         'status':'supported' if claim.claim_id=='factor-time-to-first-payment' else 'not_fact_checkable',
         'evidence_ids':[], 'reason':'Arithmetic on explicitly conditional estimates.'}
        for claim in factors.claims]})
    return factors, checked


def test_generated_total_is_arithmetic_while_inputs_remain_conditional_and_immutable():
    factors,checked = fixture()
    before = deepcopy((factors.model_dump(mode='json'),checked.model_dump(mode='json')))
    validate_factor_verification(checked,factors,market='US',milestones=True)
    assert (factors.model_dump(mode='json'),checked.model_dump(mode='json'))==before
    inputs = {factors.preparation_claim_id,factors.payment_cycle_claim_id}
    assert all(item.status=='not_fact_checkable' for item in checked.items if item.claim_id in inputs)
    assert factors.resources=='conditional'


def test_total_rounds_each_conditional_stage_up_separately():
    raw = milestone_output({'eligibility_conditions':[]})
    for field,identifier in [('preparation_days','factor-preparation'),('payment_cycle_days','factor-payment')]:
        raw[field]={'low':'1.1','high':'2.1'}
        claim = next(c for c in raw['claims'] if c['claim_id']==identifier)
        claim['quantity_estimate']=dict(raw[field])
        claim['neutral_text']='The conditional stage is estimated at 1.1 to 2.1 calendar days.'
        claim['assumptions']=[claim['neutral_text']]
    factors=materialize_milestones(FactorsV153.model_validate(raw),'US')
    _,checked=fixture()
    validate_factor_verification(checked,factors,market='US',milestones=True)
    assert factors.days.model_dump(mode='json')=={'low':'4','high':'6'}


@pytest.mark.parametrize('defect',['total_range','formula','dependencies','metric','start_event','end_event',
                                  'input_quantity','input_start','input_end','missing_input'])
def test_arithmetic_exemption_rejects_changed_total_or_inconsistent_stage_passports(defect):
    factors,checked=fixture()
    raw=factors.model_dump(mode='json')
    total=next(c for c in raw['claims'] if c['claim_id']=='factor-time-to-first-payment')
    preparation=next(c for c in raw['claims'] if c['claim_id']==raw['preparation_claim_id'])
    if defect=='total_range':
        total['quantity_estimate']['low']='1'
    elif defect=='formula':
        total['formula']='ceil(preparation.low + payment_cycle.low)'
    elif defect=='dependencies':
        total['input_claim_ids'].reverse()
    elif defect=='metric':
        total['metric']='price_in_usd'
    elif defect=='start_event':
        total['start_event']='offer_ready'
    elif defect=='end_event':
        total['end_event']='offer_ready'
    elif defect=='input_quantity':
        preparation['quantity_estimate']={'low':'1','high':'2'}
    elif defect=='input_start':
        preparation['start_event']='offer_ready'
    elif defect=='input_end':
        preparation['end_event']='first_payment_received'
    else:
        raw['claims']=[c for c in raw['claims'] if c['claim_id']!=raw['preparation_claim_id']]
        checked.items=[i for i in checked.items if i.claim_id!=raw['preparation_claim_id']]
    with pytest.raises(ValueError):
        validate_factor_verification(checked,FactorsStateV153.model_validate(raw),market='US',milestones=True)


@pytest.mark.parametrize('status',['refuted','mixed','insufficient'])
def test_unacceptable_input_review_cannot_unlock_supported_arithmetic(status):
    factors,checked=fixture()
    item=next(i for i in checked.items if i.claim_id==factors.preparation_claim_id)
    item.status=status
    item.evidence_ids=['evidence'] if status in {'refuted','mixed'} else []
    with pytest.raises(ValueError):
        validate_factor_verification(checked,factors,market='US',milestones=True)


@pytest.mark.parametrize('kind',['fact','calculation'])
def test_other_source_free_supported_claim_still_rejects_whole_candidate(kind):
    factors,checked=fixture()
    raw=factors.model_dump(mode='json')
    other=deepcopy(raw['claims'][0])
    other.update(claim_id='arbitrary-claim',kind=kind,method=kind if kind=='calculation' else 'primary_source',
                 formula='x+y' if kind=='calculation' else None,
                 input_claim_ids=[raw['preparation_claim_id']] if kind=='calculation' else [],
                 source_urls=[],evidence_refs=[])
    raw['claims'].insert(-1,other)  # The generated total remains last, as in real materialization.
    candidate=Verification.model_validate({'items':checked.model_dump(mode='json')['items']+
        [{'claim_id':'arbitrary-claim','status':'supported','evidence_ids':[],'reason':'No source supplied.'}]})
    with pytest.raises(ValueError,match='evidence'):
        validate_factor_verification(candidate,FactorsStateV153.model_validate(raw),market='US',milestones=True)


def test_legacy_path_has_no_reserved_arithmetic_exemption():
    factors,checked=fixture()
    with pytest.raises(ValueError,match='evidence'):
        validate_factor_verification(checked,factors,market='US',milestones=False)
