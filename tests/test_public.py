from copy import deepcopy
from types import SimpleNamespace
import json

from fastapi.testclient import TestClient
import pytest

from ai_builder.api import create_app
from ai_builder.public import (Model, load_featured_followup, load_snapshot, public_model,
                               publish_featured_followup, publish_snapshot)
from ai_builder.pipeline import Document, ScopePacket


def result_fixture():
    document = Document(snapshot_id='snapshot-a', evidence_id='evidence-a', url='https://example.com/source',
                        text='PRIVATE ORIGINAL BODY NEVER PUBLISHED', content_hash='fixture', policy_status='validated')
    scope = ScopePacket(scope_id='scope-a', base_model_id='base-a', variant_id='variant-a',
                        neutral_model_description='Proposed scheduling software', documents=[document])
    claim = dict(claim_id='fact-a', neutral_text='Subscriptions repeat monthly.', kind='fact', method='primary_source',
                 confidence='medium', confidence_reason='The source states a monthly term.', entity='Example', geography='US',
                 period=None, metric='subscription term', formula=None, input_claim_ids=[], assumptions=[],
                 evidence_refs=[dict(source_snapshot_id='snapshot-a', evidence_id='evidence-a',
                                     excerpt='A monthly subscription.', locator='Pricing paragraph')])
    plan = {**claim, 'claim_id': 'plan-a', 'neutral_text': 'Interview potential customers before building.',
            'kind': 'judgment', 'method': 'model_judgment', 'confidence': None, 'confidence_reason': None,
            'assumptions': ['The team can contact customers.'], 'evidence_refs': []}
    state = dict(
        private_provider_payload={'secret': 'NEVER-EXPOSE-THIS'},
        card={'eligibility': 'pass', 'publication': 'ready', 'publication_blockers': [],
              'validated_at': '2026-11-16T10:00:00Z',
              'score': {'status': 'final', 'value': '45', 'bounds': {'low': '45', 'high': '65'}, 'goal_feasibility': 'uncertain'}},
        criteria=[dict(criterion_id=f'F{i:02}', result='pass', reason='Supported for this scenario.',
                       evidence_ids=['evidence-a'], confidence='medium', confidence_reason='Source is relevant.') for i in range(1, 11)],
        critique={'claims': [claim], 'risks': [dict(risk_id='risk-a', premise_claim_ids=['fact-a'])], 'limitations': []},
        factors={'claims': [plan], 'factors': [dict(factor_id='S01', value={'low': 1, 'high': 3}, claim_ids=['plan-a'])],
                 'days': {'low': 30, 'high': 60}, 'cost': {'low': 100, 'high': 500}, 'test_plan_claim_ids': ['plan-a']},
        verification={'items': [{'claim_id': 'fact-a', 'status': 'supported'}]},
        factor_verification={'items': [{'claim_id': 'plan-a', 'status': 'not_fact_checkable'}]},
        source_metadata={'snapshot-a': {'title': 'Source pricing', 'publisher': 'Example', 'published_at': None,
                                      'fetched_at': '2026-11-16T09:00:00Z', 'raw_artifact_ref': 'PRIVATE-FILE-PATH'}},
    )
    return SimpleNamespace(prepared=SimpleNamespace(scope=scope), state=state, usage={'private': 'BILLING-SECRET'})


def metadata(identifier='model-a'):
    return dict(id=identifier, title='Scheduling software', category='Workflows', summary='Organise appointments online.',
                buyer='Small consulting teams', revenue='Monthly subscriptions', comparable='Example',
                discovery_sources=[dict(url='https://example.com/lead', title='Discovery lead', query='subscription model')],
                private_key='DO-NOT-EXPOSE-METADATA')


def snapshot(models):
    return dict(schema_version='prototype-1', run_id='collection-a', revision_id='revision-a', title='The US launch collection',
                market='US', created_at='2026-11-16T09:00:00Z', published_at='2026-11-16T10:00:00Z',
                expected_count=20, status='partial', stage='Research in progress',
                scenario={'team_size': 2, 'business_budget_usd': '15000', 'days': 90}, models=models)


def test_projection_preserves_actual_passports_and_drops_private_state(config):
    model = public_model(result_fixture(), metadata(), config)
    rendered = json.dumps(model)
    for value in ('PRIVATE ORIGINAL', 'NEVER-EXPOSE', 'BILLING-SECRET', 'PRIVATE-FILE-PATH', 'DO-NOT-EXPOSE'):
        assert value not in rendered
    assert model['eligible_for_ranking']
    assert model['claims'][0]['confidence'] == 'medium'
    assert model['claims'][0]['method'] == 'primary_source'
    assert model['claims'][0]['checked_at'] == '2026-11-16T10:00:00Z'
    assert model['claims'][1]['confidence'] is None
    assert model['claims'][1]['assumptions'] == ['The team can contact customers.']
    assert model['test_plan_claim_ids'] == ['plan-a']
    assert model['sources'][0]['published_at'] is None
    assert model['discovery_sources'][0]['title'] == 'Discovery lead'


def test_gate_blocks_provisional_score_and_missing_mandatory_checks(config):
    result = result_fixture()
    result.state['card']['publication_blockers'] = ['needs_manual_check']
    provisional = public_model(result, metadata(), config)
    assert not provisional['eligible_for_ranking'] and provisional['rank'] is None
    assert provisional['score']['status'] == 'provisional'
    result.state['card']['publication_blockers'] = []
    result.state['criteria'][0]['result'] = 'unknown'
    with pytest.raises(ValueError, match='mandatory criterion'):
        public_model(result, metadata(), config)


def test_public_score_cannot_misrepresent_interval(config):
    model = public_model(result_fixture(), metadata(), config)
    model['score']['high'] = 40
    with pytest.raises(ValueError, match='within its interval'):
        Model.model_validate(model)


def test_publishing_keeps_group_ranking_and_ties_and_marks_partial(config, tmp_path):
    models = []
    for identifier, score, goal in [('one', 40, 'uncertain'), ('two', 40, 'uncertain'), ('three', 60, 'uncertain'), ('four', 10, 'feasible')]:
        model = public_model(result_fixture(), metadata(identifier), config)
        model['score'].update(value=score, low=score, high=score + 10, goal_feasibility=goal)
        models.append(model)
    models.append(public_model(None, {**metadata('pending'), 'status': 'queued'}, config))
    publish_snapshot(tmp_path, snapshot(models))
    saved = load_snapshot(tmp_path)
    assert saved['selected_count'] == 5 and saved['completed_count'] == 4
    assert [model['rank'] for model in saved['models']] == [1, 1, 3, 1, None]
    value = snapshot(models)
    value['status'] = 'completed'
    with pytest.raises(ValueError, match='partial'):
        publish_snapshot(tmp_path, value)


def test_invalid_private_snapshot_and_path_cannot_be_served(config, tmp_path):
    publish_snapshot(tmp_path, snapshot([public_model(result_fixture(), metadata(), config)]))
    assert load_snapshot(tmp_path, '../../secrets') is None
    path = tmp_path / 'public' / 'collection-a.json'
    contents = json.loads(path.read_text())
    contents['provider_payload'] = {'secret': 'must-not-appear'}
    path.write_text(json.dumps(contents))
    assert load_snapshot(tmp_path) is None


def test_public_pages_assets_compare_limits_and_read_only_routes(config, tmp_path):
    models = [public_model(result_fixture(), metadata(f'model-{i}'), config) for i in range(4)]
    models[0]['title'] = '<script>alert(1)</script>'
    publish_snapshot(tmp_path, snapshot(models))
    app = create_app(data_dir=tmp_path, database_url='', technical_preview=False)
    with TestClient(app) as client:
        for url in ('/', '/runs/collection-a', '/models/model-0?run=collection-a', '/runs/collection-a/models/model-0',
                    '/methodology', '/compare?run=collection-a&models=model-0,model-1,model-2',
                    '/runs/collection-a/compare?models=model-0,model-1', '/assets/public.css', '/assets/public.js'):
            response = client.get(url)
            assert response.status_code == 200, (url, response.text)
            assert '<script>alert(1)</script>' not in response.text
        assert client.head('/runs/collection-a').status_code == 200
        collection = client.get('/runs/collection-a').text
        for label in ('Passed', 'Filtered out', 'Needs review', 'Unfinished', 'assessments processed'):
            assert label in collection
        detail = client.get('/runs/collection-a/models/model-0').text
        assert 'A plan to test demand.' in detail and 'Interview potential customers before building.' in detail
        assert 'Confidence' in detail and 'Not assessed' in detail
        assert client.get('/compare?run=collection-a&models=model-0,model-1,model-2,model-3').status_code == 400
        assert client.get('/compare?run=collection-a&models=another-run-model').status_code == 400
        assert client.get('/compare?run=collection-a&models=model-0&revision=outdated').status_code == 409
        assert client.get('/models/missing?run=collection-a').status_code == 404
        assert client.post('/runs/collection-a').status_code == 405
        assert client.get('/public/collection-a.json').status_code == 404
        assert client.get('/evidence/private-file').status_code == 404
        data = client.get('/runs/collection-a?format=json').json()
        assert data['selected_count'] == 4
        assert 'usage' not in data


def test_unknown_result_has_no_empty_unperformed_sections(config, tmp_path):
    result = result_fixture()
    result.state = {'card': {'eligibility': 'needs_review', 'publication': 'draft', 'score': None},
                    'criteria': [{'criterion_id': 'F01', 'result': 'unknown', 'reason': 'Jurisdiction not supplied.', 'evidence_ids': []}]}
    publish_snapshot(tmp_path, snapshot([public_model(result, metadata(), config)]))
    with TestClient(create_app(data_dir=tmp_path, database_url='')) as client:
        response = client.get('/runs/collection-a/models/model-a')
        assert response.status_code == 200
        assert 'Critic, Verifier and scoring wait' in response.text
        for unused in ('id="risks"', 'id="factors"', 'id="claims"', 'id="test-plan"'):
            assert unused not in response.text
        assert 'id="criteria"' in response.text and 'id="sources"' in response.text


def test_empty_site_is_useful_without_database_or_published_research(tmp_path):
    with TestClient(create_app(data_dir=tmp_path, database_url='')) as client:
        assert client.get('/').status_code == 200
        assert 'The first collection is on its way.' in client.get('/').text
        assert client.get('/methodology').status_code == 200
        assert client.get('/runs/missing').status_code == 404


def focused_snapshot(config):
    value = snapshot([public_model(result_fixture(), metadata('focused-model'), config)])
    value.update(run_id='focused-run', revision_id='focused-revision', expected_count=1, status='completed',
                 title='A focused helpdesk launch', parent_collection_id='collection-a',
                 parent_run_id='original-child-run', parent_model_id='model-a',
                 launch_conditions=['B2B email support only; no custody of customer funds.'])
    value['scenario']['jurisdiction'] = 'Texas / Austin'
    return value


def test_focused_publication_preserves_latest_and_checks_parent_membership(config, tmp_path):
    publish_snapshot(tmp_path, snapshot([public_model(result_fixture(), metadata(), config)]))
    focused = focused_snapshot(config)
    with pytest.raises(ValueError, match='main collection'):
        publish_snapshot(tmp_path, focused)
    publish_snapshot(tmp_path, focused, set_latest=False)
    publish_featured_followup(tmp_path, 'focused-run')
    assert load_snapshot(tmp_path)['run_id'] == 'collection-a'
    assert load_featured_followup(tmp_path)['run_id'] == 'focused-run'
    for changed in ({'parent_model_id': 'not-a-member'}, {'parent_collection_id': 'missing-parent'}):
        invalid = {**focused, **changed, 'run_id': 'invalid-followup'}
        publish_snapshot(tmp_path, invalid, set_latest=False)
        with pytest.raises(ValueError, match='same base identity'):
            publish_featured_followup(tmp_path, 'invalid-followup')
    mismatched = deepcopy(focused)
    mismatched['run_id'] = 'another-base'
    mismatched['models'][0]['base_model_id'] = 'different-base'
    publish_snapshot(tmp_path, mismatched, set_latest=False)
    with pytest.raises(ValueError, match='same base identity'):
        publish_featured_followup(tmp_path, 'another-base')
    assert load_featured_followup(tmp_path)['run_id'] == 'focused-run'


def test_publication_retains_previous_revision_before_replacement(config, tmp_path):
    value = snapshot([public_model(result_fixture(), metadata(), config)])
    publish_snapshot(tmp_path, value)
    original = load_snapshot(tmp_path)
    value.update(revision_id='revision-b', title='Updated research')
    publish_snapshot(tmp_path, value)
    history = tmp_path / 'public' / 'history' / 'collection-a' / 'revision-a.json'
    assert json.loads(history.read_text()) == original
    assert load_snapshot(tmp_path)['revision_id'] == 'revision-b'
    path = tmp_path / 'public' / 'collection-a.json'
    path.write_text('{invalid')
    with pytest.raises(ValueError, match='invalid public snapshot'):
        publish_snapshot(tmp_path, value)
    assert path.read_text() == '{invalid'
    assert json.loads(history.read_text()) == original


def test_featured_pointer_cannot_read_paths_or_unlinked_snapshots(config, tmp_path):
    publish_snapshot(tmp_path, snapshot([public_model(result_fixture(), metadata(), config)]))
    pointer = tmp_path / 'public' / 'featured.json'
    for value in ({'run_id': '../../private'}, {'run_id': 'collection-a'}, {'path': 'private.json'}, []):
        pointer.write_text(json.dumps(value))
        assert load_featured_followup(tmp_path) is None
    with pytest.raises(ValueError, match='Reserved'):
        publish_snapshot(tmp_path, {**snapshot([]), 'run_id': 'featured'})


def test_design_basis_is_visible_without_private_scope_identity(config, tmp_path):
    result = result_fixture()
    result.state['criteria'][1]['design_basis'] = [dict(kind='proposed_design',
        assessment_scope_id='PRIVATE-SCOPE-ID', excerpt='The proposed service is delivered entirely online.')]
    model = public_model(result, metadata(), config)
    assert model['criteria'][1]['design_basis'] == [dict(kind='proposed_design',
        excerpt='The proposed service is delivered entirely online.')]
    assert 'PRIVATE-SCOPE-ID' not in json.dumps(model)
    publish_snapshot(tmp_path, snapshot([model]))
    with TestClient(create_app(data_dir=tmp_path, database_url='')) as client:
        text = client.get('/runs/collection-a/models/model-a').text
        assert 'Proposed design' in text
        assert 'not independently observed external facts' in text
        assert 'The proposed service is delivered entirely online.' in text


def test_focused_pages_show_scenario_conditions_links_and_block_mixed_comparison(config, tmp_path):
    parent_models = [public_model(result_fixture(), metadata(), config),
                     public_model(result_fixture(), metadata('other-model'), config)]
    publish_snapshot(tmp_path, snapshot(parent_models))
    publish_snapshot(tmp_path, focused_snapshot(config), set_latest=False)
    publish_featured_followup(tmp_path, 'focused-run')
    with TestClient(create_app(data_dir=tmp_path, database_url='')) as client:
        link = '/runs/focused-run/models/focused-model'
        home = client.get('/').text
        assert link in home and '/runs/collection-a' in home
        assert 'Read the focused analysis' in client.get('/runs/collection-a/models/model-a').text
        assert 'Read the focused analysis' not in client.get('/runs/collection-a/models/other-model').text
        detail = client.get(link).text
        for expected in ('Texas / Austin', '2 people', '$15,000', '90 days', 'excluding salaries',
                         'Conditions of this proposed launch', 'B2B email support only',
                         'do not confirm', 'See the original assessment'):
            assert expected in detail
        assert client.get('/runs/collection-a/compare?models=model-a,focused-model').status_code == 400
        assert client.get('/runs/focused-run/compare?models=focused-model,model-a').status_code == 400
        assert client.get('/runs/focused-run').status_code == 200
