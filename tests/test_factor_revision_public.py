"""Five updated assessments coexist with fifteen unchanged historical payloads."""
from copy import deepcopy
import json

from fastapi.testclient import TestClient
import pytest

from ai_builder.api import create_app
from ai_builder.public import Model, Timing, load_snapshot, model_digest, public_model, publish_snapshot
from test_conditional_public import conditional_result, versioned_snapshot
from test_public import metadata, result_fixture, snapshot, focused_snapshot
from ai_builder.public import publish_featured_followup


def milestone_result():
    result = conditional_result()
    base = result.state['factors']['claims'][0]
    timing = {}
    for key, identifier, start, end, estimate, days in (
        ('preparation', 'prep', 'assessment_start', 'offer_ready', (2.1, 4.8), (3, 5)),
        ('payment_cycle', 'payment', 'offer_ready', 'first_payment_received', (5.2, 8), (6, 8)),
        ('total', 'total', 'assessment_start', 'first_payment_received', (9, 13), (9, 13)),
    ):
        result.state['factors']['claims'].append({**base, 'claim_id': identifier,
            'neutral_text': f'Synthetic {key} estimate.', 'start_event': start, 'end_event': end,
            'kind': 'calculation' if key == 'total' else 'judgment',
            'formula': 'ceil(preparation) + ceil(payment_cycle)' if key == 'total' else None,
            'input_claim_ids': ['prep', 'payment'] if key == 'total' else []})
        timing[key] = dict(estimate=dict(zip(('low', 'high'), estimate)), days=dict(zip(('low', 'high'), days)),
                           claim_id=identifier, start_event=start, end_event=end)
    result.state['factors']['factors'] += [dict(factor_id='S03', value={'low': 3, 'high': 4},
        claim_ids=['prep'], rationale='Synthetic author score rationale; excluded from verifier text.')]
    result.state['factors']['days'] = timing['total']['days']
    result.state['card'].update(scoring_policy_version='1.5.3', timing=timing,
        inherited_stages=['research', 'applicability', 'critic', 'neutrality', 'verifier', 'reconciliation'])
    return result


def revision_catalog(config, tmp_path):
    original = snapshot([public_model(result_fixture(), metadata(f'model-{i}'), config) for i in range(20)])
    original['status'] = 'completed'
    publish_snapshot(tmp_path, original)
    original = load_snapshot(tmp_path)
    pending = [public_model(None, {**metadata(m['id']), 'base_model_id': m['base_model_id'],
        'scoring_policy_version': '1.5.2', 'queue_position': i + 1}, config) for i, m in enumerate(original['models'])]
    middle = versioned_snapshot(pending)
    publish_snapshot(tmp_path, middle)
    selected = [m['id'] for m in original['models'][:5]]
    value = snapshot(deepcopy(original['models']))
    value.update(run_id='factor-revision', revision_id='factor-revision-1', scoring_policy_version='1.5.3',
        reassessment_of_collection_id='reassessment-a', expected_count=5, catalog_count=20,
        selected_model_ids=selected, processing_order=selected, update_states={m: 'queued' for m in selected},
        retained_assessments={m['id']: dict(run_id=original['run_id'], revision_id=original['revision_id'],
            scoring_policy_version=m.get('scoring_policy_version'), model_hash=model_digest(m)) for m in original['models']})
    return original, value


def replace_selected(value, config, index=0):
    model = public_model(milestone_result(), metadata(f'model-{index}'), config)
    value['models'][index] = model
    value['retained_assessments'].pop(model['id'])
    value['update_states'][model['id']] = 'completed'
    return model


def test_selective_envelope_preserves_twenty_payloads_and_counts_five_updates(config, tmp_path):
    original, value = revision_catalog(config, tmp_path)
    before = (tmp_path / 'public' / 'collection-a.json').read_bytes()
    publish_snapshot(tmp_path, value)
    saved = load_snapshot(tmp_path)
    assert saved['models'] == original['models']
    assert saved['catalog_count'] == 20 and saved['expected_count'] == saved['selected_count'] == 5
    assert saved['completed_count'] == 0
    assert (tmp_path / 'public' / 'collection-a.json').read_bytes() == before
    replace_selected(value, config)
    value['revision_id'] = 'factor-revision-2'
    publish_snapshot(tmp_path, value)
    saved = load_snapshot(tmp_path)
    assert saved['completed_count'] == 1 and saved['models'][0]['rank'] == 1
    assert saved['models'][1:] == original['models'][1:]
    assert saved['models'][5]['rank'] == original['models'][5]['rank']  # Historical payload, never a new rank.
    assert load_snapshot(tmp_path, 'factor-revision', revision_id='factor-revision-1')['completed_count'] == 0


@pytest.mark.parametrize('defect', ['six_selected', 'unselected_without_ref', 'payload_changed', 'wrong_hash',
                                  'wrong_policy', 'wrong_update_state', 'unknown_ref', 'wrong_ref_revision'])
def test_retention_and_selected_manifest_fail_closed(config, tmp_path, defect):
    _, value = revision_catalog(config, tmp_path)
    if defect == 'six_selected':
        value['selected_model_ids'].append('model-5')
    elif defect == 'unselected_without_ref':
        value['retained_assessments'].pop('model-5')
    elif defect == 'payload_changed':
        value['models'][5]['summary'] = 'Rewritten old analysis'
    elif defect == 'wrong_hash':
        value['retained_assessments']['model-5']['model_hash'] = '0' * 64
    elif defect == 'wrong_policy':
        value['retained_assessments']['model-5']['scoring_policy_version'] = '1.5.3'
    elif defect == 'wrong_update_state':
        value['update_states']['model-0'] = 'completed'
    elif defect == 'unknown_ref':
        value['retained_assessments']['model-5']['run_id'] = 'unrelated'
    else:
        value['retained_assessments']['model-5']['revision_id'] = 'unpublished'
    with pytest.raises(ValueError):
        publish_snapshot(tmp_path, value)


def test_selected_and_unselected_pages_preserve_evidence_without_mixing_scores(config, tmp_path):
    original, value = revision_catalog(config, tmp_path)
    value['update_states']['model-0'] = 'running'
    publish_snapshot(tmp_path, value)
    focused = focused_snapshot(config)
    focused['parent_model_id'] = 'model-6'
    publish_snapshot(tmp_path, focused, set_latest=False)
    publish_featured_followup(tmp_path, 'focused-run')
    with TestClient(create_app(data_dir=tmp_path, database_url='')) as client:
        home = client.get('/').text
        assert '20 catalog models · 5 selected updates' in home and 'View all 20 models' in home
        catalogue = client.get('/runs/factor-revision').text
        assert '0 of 5 selected updates processed' in catalogue and '20 of 20 models shown' in catalogue
        assert 'Previous findings retained' in catalogue
        assert 'card-score' not in catalogue and 'data-compare-id=' not in catalogue
        unselected = client.get('/models/model-6').text
        assert 'Not selected for this update' in unselected and 'Source pricing' in unselected
        assert '45–65' not in unselected and 'No new score for this update' in unselected
        assert 'focused-run' in unselected  # Original ancestor remains linked two generations later.
        pending = client.get('/models/model-0').text
        assert 'Previous assessment — update in progress' in pending and 'Subscriptions repeat monthly.' in pending
        assert 'Previous criterion' not in pending
        filtered = client.get('/runs/factor-revision?status=not_selected').text
        assert '15 of 20 models shown' in filtered and 'data-compare-id=' not in filtered
        assert client.get('/compare?run=factor-revision&revision=factor-revision-1&models=model-6').status_code == 409
        assert client.get('/models/model-0?run=collection-a').text.count('Follow the current reassessment') == 1
        replace_selected(value, config)
        value['revision_id'] = 'factor-revision-2'
        publish_snapshot(tmp_path, value)
        detail = client.get('/models/model-0').text
        for label in ('Preparation to a ready offer', 'Ready offer to first payment', 'Total time to first payment',
                      '3–5 days', '6–8 days', '9–13 days', '2.1–4.8 days', 'Inherited completed stages',
                      'Newly completed stages', 'Author’s score rationale', 'Milestone boundary'):
            assert label in detail, label
        assert 'Previous assessment —' not in detail
        comparison = client.get('/compare?run=factor-revision&revision=factor-revision-2&models=model-0')
        assert comparison.status_code == 200 and 'Total time to first payment' in comparison.text
        assert client.get('/compare?run=factor-revision&revision=factor-revision-2&models=model-0,model-6').status_code == 409
        assert client.get('/runs/factor-revision?format=json').json()['models'][5:] == original['models'][5:]


def test_unknown_stage_stays_unknown_and_legacy_is_not_split(config, tmp_path):
    result = milestone_result()
    result.state['card'].update(publication='draft', score=None)
    result.state['card']['timing']['payment_cycle'].update(estimate=None, days=None, claim_id=None)
    result.state['card']['timing']['total'].update(estimate=None, days=None, claim_id=None)
    model = public_model(result, metadata(), config)
    assert model['timing']['preparation']['days'] == {'low': 3, 'high': 5}
    assert model['timing']['total']['days'] is None and model['days'] is None and model['score'] is None
    legacy = public_model(result_fixture(), metadata(), config)
    assert 'timing' not in legacy and 'inherited_stages' not in legacy
    assert all('start_event' not in claim for claim in legacy['claims'])
    assert all('author_rationale' not in factor for factor in legacy['factors'])
    assert Model.model_validate(legacy).model_dump(mode='json') == legacy


@pytest.mark.parametrize('defect', ['boundary', 'rounding', 'total', 'passport'])
def test_milestone_projection_cannot_misrepresent_saved_estimates(config, defect):
    result = milestone_result()
    timing = result.state['card']['timing']
    if defect == 'boundary':
        timing['payment_cycle']['start_event'] = 'assessment_start'
    elif defect == 'rounding':
        timing['preparation']['days']['low'] = 2
    elif defect == 'total':
        timing['total'].update(estimate={'low': 90, 'high': 90}, days={'low': 90, 'high': 90})
    else:
        timing['preparation']['claim_id'] = 'missing'
    with pytest.raises(ValueError):
        public_model(result, metadata(), config)


@pytest.mark.skipif(not __import__('os').environ.get('TEST_DATABASE_URL'), reason='Requires isolated PostgreSQL')
@pytest.mark.parametrize('defect', [None, 'unknown'])
def test_real_milestone_card_projects_exact_handoff_and_keeps_unknowns(defect):
    from types import SimpleNamespace
    from test_milestone_pipeline import MilestoneRoles, run_milestone_case

    async def project(ledger, config, scope, roles, state):
        model = public_model(SimpleNamespace(state=state, prepared=SimpleNamespace(scope=scope)), metadata(), config)
        assert model['scoring_policy_version'] == '1.5.3'
        assert model['timing'] == Timing.model_validate(state['card']['timing']).model_dump(mode='json')
        assert model['days'] == model['timing']['total']['days']
        claims = {claim['id']: claim for claim in model['claims']}
        for handed_off in state.get('factor_handoff', []):
            projected = claims[handed_off['claim_id']]
            assert projected['text'] == handed_off['neutral_text']
            assert projected.get('start_event') == handed_off.get('start_event')
            assert projected.get('end_event') == handed_off.get('end_event')
            assert 'AUTHOR-ONLY' not in projected['text']
        assert model['factors'][0]['author_rationale'].startswith('AUTHOR-ONLY')
        if defect:
            assert model['score'] is None and model['timing']['total']['days'] is None
        else:
            assert model['score']['status'] == 'conditional' and model['eligible_for_ranking']
            assert model['score']['value'] == 66.25
            assert model['timing']['total']['days'] == {'low': 29, 'high': 45}
            assert claims['factor-time-to-first-payment']['input_claim_ids'] == ['factor-preparation', 'factor-payment']

    run_milestone_case(MilestoneRoles(defect), project)
