"""The public contract keeps conditional results separate from confirmed history."""
from copy import deepcopy
import json

from fastapi.testclient import TestClient
import pytest

from ai_builder.api import create_app
from ai_builder.public import Model, PublicSnapshot, load_snapshot, public_model, publish_snapshot
from test_public import metadata, result_fixture, snapshot


def conditional_result(*, conditional=True, coverage='reviewed'):
    result = result_fixture()
    card = result.state['card']
    card.update(eligibility='conditional' if conditional else 'confirmed', scoring_policy_version='1.5.2',
                completed_stages=['research', 'applicability', 'critic', 'neutrality', 'verifier',
                                  'reconciliation', 'factors', 'factor_validation', 'card_validation'],
                eligibility_conditions=[], legal_reviews=[dict(
                    analogue_id='example', company_name='Example', execution_state='completed', coverage=coverage,
                    supplier_role='The vendor supplies subscription software.',
                    our_role_applicability='Our proposed role is software supply, not the vendor legal entity.',
                    source_manifest_ref='PRIVATE-LEGAL-MANIFEST',
                    documents=[dict(url='https://example.com/terms', document_type='terms', snapshot_id='snapshot-a',
                                    evidence_id='evidence-a', disposition='read', title='Terms',
                                    raw_artifact_ref='PRIVATE-LEGAL-BODY')],
                    clauses=[dict(evidence_id='evidence-a', source_snapshot_id='snapshot-a',
                                  excerpt='The customer uses subscription software.', locator='Section 2',
                                  finding='This describes the supplier relationship, not our compliance.')],
                    search_actions=[dict(action='search', query='Example official legal terms',
                                         outcome='found', provider_payload='PRIVATE-SEARCH-PAYLOAD')],
                    gaps=['State-specific obligations are not established.'],
                    analysis_gaps=['Our launch contracts are not yet signed.'])])
    card['score']['status'] = 'conditional' if conditional else 'final'
    if conditional:
        result.state['criteria'][0]['result'] = 'unknown'
        card['eligibility_conditions'] = [dict(condition_id='lawful-check', criterion_id='F01',
            assumption='No applicable prohibition has been established for the described role.',
            required_check='Confirm the applicable legal requirements before launch.',
            impact_on_factors='S03 and S05 may change if legal work adds time or cost.', evidence_ids=['evidence-a'])]
    return result


def versioned_snapshot(models):
    value = snapshot(models)
    value.update(run_id='reassessment-a', revision_id='reassessment-revision-a', scoring_policy_version='1.5.2',
                 reassessment_of_collection_id='collection-a', processing_order=[m['id'] for m in models])
    return value


def test_conditional_projection_exposes_conditions_actual_stages_and_legal_evidence(config):
    model = public_model(conditional_result(), metadata(), config)
    assert model['status'] == 'conditional' and model['score']['status'] == 'conditional'
    assert model['eligible_for_ranking']
    assert model['criteria'][0]['result'] == 'unknown'
    assert model['eligibility_conditions'][0]['source_ids'] == ['snapshot-a']
    review = model['legal_reviews'][0]
    assert review['coverage'] == 'reviewed' and len(review['gaps']) == 2
    assert review['clauses'][0]['source_id'] == 'snapshot-a'
    assert review['documents'][0]['disposition'] == 'read'
    assert model['completed_stages'][-1] == 'card_validation'
    serialized = json.dumps(model)
    assert 'PRIVATE-' not in serialized
    assert 'provider_payload' not in serialized


def test_legal_gaps_show_excluded_count_and_external_host_without_private_artifacts(config):
    result = conditional_result()
    result.state['card']['legal_reviews'][0]['gaps'] = [
        {'code':'outside_bounded_review', 'document_count':296, 'exclusion_manifest_ref':'PRIVATE-EXCLUSIONS'},
        {'code':'external_redirect_attribution_unverified', 'url':'https://example.com/terms',
         'final_url':'https://legal-host.example/terms', 'snapshot_id':'PRIVATE-SNAPSHOT'}]
    value = public_model(result, metadata(), config)
    gaps = value['legal_reviews'][0]['gaps']
    assert any('296 documents' in gap for gap in gaps)
    assert any('https://legal-host.example/terms' in gap for gap in gaps)
    assert 'PRIVATE-' not in json.dumps(value)


@pytest.mark.parametrize('coverage', ['insufficient', 'not_found', 'unavailable'])
def test_completed_document_gaps_do_not_prevent_conditional_ranking(config, coverage):
    model = public_model(conditional_result(coverage=coverage), metadata(), config)
    assert model['eligible_for_ranking'] and model['legal_reviews'][0]['coverage'] == coverage


def test_unperformed_reviews_and_unexplained_unknowns_cannot_be_ranked(config):
    base = public_model(conditional_result(), metadata(), config)
    for change in ('missing_review', 'pending_review', 'missing_condition', 'mandatory_unknown', 'score_mismatch'):
        model = deepcopy(base)
        if change == 'missing_review':
            model['legal_reviews'] = []
        elif change == 'pending_review':
            model['legal_reviews'][0].update(execution_state='pending', coverage=None)
        elif change == 'missing_condition':
            model['eligibility_conditions'] = []
        elif change == 'mandatory_unknown':
            model['criteria'][1]['result'] = 'unknown'
        else:
            model['score']['status'] = 'final'
        with pytest.raises(ValueError):
            Model.model_validate(model)


def test_new_collection_keeps_separate_ranks_and_original_history(config, tmp_path):
    original = snapshot([public_model(result_fixture(), metadata('old'), config)])
    publish_snapshot(tmp_path, original)
    old_bytes = (tmp_path/'public'/'collection-a.json').read_bytes()
    models = []
    for identifier, conditional, score, goal in [('final-one', False, 55, 'uncertain'),
              ('conditional-one', True, 10, 'uncertain'), ('conditional-two', True, 10, 'uncertain'),
              ('conditional-three', True, 60, 'uncertain'), ('conditional-four', True, 10, 'feasible')]:
        model = public_model(conditional_result(conditional=conditional), metadata(identifier), config)
        model['score'].update(value=score, low=score, high=score + 10, goal_feasibility=goal)
        models.append(model)
    pending = public_model(None, {**metadata('pending'), 'scoring_policy_version': '1.5.2', 'queue_position': 6}, config)
    models.append(pending)
    publish_snapshot(tmp_path, versioned_snapshot(models))
    current = load_snapshot(tmp_path)
    assert current['completed_count'] == 5
    assert [model['rank'] for model in current['models']] == [1, 1, 1, 3, 1, None]
    assert current['models'][-1]['score'] is None
    assert current['models'][-1]['completed_stages'] == []
    assert (tmp_path/'public'/'collection-a.json').read_bytes() == old_bytes
    assert 'scoring_policy_version' not in load_snapshot(tmp_path, 'collection-a')
    assert 'scoring_policy_version' not in load_snapshot(tmp_path, 'collection-a')['models'][0]


def test_reassessment_cannot_mix_policies_or_reuse_old_pending_scores(config):
    old = public_model(result_fixture(), metadata('old'), config)
    new = public_model(conditional_result(), metadata('new'), config)
    with pytest.raises(ValueError, match='mix scoring policies'):
        PublicSnapshot.model_validate(versioned_snapshot([old, new]))
    queued = {**new, 'status': 'queued', 'eligible_for_ranking': False}
    with pytest.raises(ValueError, match='reuse an old score'):
        Model.model_validate(queued)
    without_policy = deepcopy(new)
    without_policy.pop('scoring_policy_version')
    with pytest.raises(ValueError, match='scoring policy'):
        Model.model_validate(without_policy)


def test_conditional_pages_and_comparison_are_revision_bound(config, tmp_path):
    old = public_model(result_fixture(), metadata(), config)
    publish_snapshot(tmp_path, snapshot([old]))
    conditional = public_model(conditional_result(), metadata(), config)
    confirmed = public_model(conditional_result(conditional=False), metadata('confirmed'), config)
    publish_snapshot(tmp_path, versioned_snapshot([conditional, confirmed]))
    with TestClient(create_app(data_dir=tmp_path, database_url='')) as client:
        collection = client.get('/runs/reassessment-a').text
        assert 'The conditional ranking.' in collection and 'The final ranking.' in collection
        assert '1 conditional assessments' in collection
        assert 'View the previous collection' in collection
        historical = client.get('/runs/collection-a').text
        assert 'Follow the current reassessment' in historical
        detail = client.get('/models/model-a?run=reassessment-a').text
        for content in ('Conditional assessment', 'Conditions before launch.', 'Legal documents.',
                        'What has been completed.', 'Factor premise verification', 'Terms', 'Section 2',
                        'State-specific obligations', 'Our launch contracts', 'not our compliance',
                        'Completed · Reviewed', 'Implications for the assessment: S03'):
            assert content in detail, content
        assert 'Critic, Verifier and scoring wait until mandatory checks pass' not in detail
        assert 'Every criterion must pass' not in detail
        assert 'PRIVATE-' not in detail
        url = '/compare?run=reassessment-a&models=model-a,confirmed'
        assert client.get(url).status_code == 409
        assert client.get(url + '&revision=older').status_code == 409
        comparison = client.get(url + '&revision=reassessment-revision-a')
        assert comparison.status_code == 200
        assert 'Conditional assessment' in comparison.text and 'Final assessment' in comparison.text
        assert 'Launch conditions' in comparison.text and 'Legal document review' in comparison.text
        assert client.get('/models/model-a?run=collection-a').text.count('Follow the current reassessment') == 1


@pytest.mark.skipif(not __import__('os').environ.get('TEST_DATABASE_URL'), reason='Requires isolated PostgreSQL')
@pytest.mark.parametrize('coverage', ['insufficient', 'not_found', 'unavailable'])
def test_real_conditional_pipeline_card_projects_and_publishes(coverage, tmp_path):
    from types import SimpleNamespace
    from test_conditional_pipeline import ConditionalRoles, conditional_packet, run_case

    async def project(ledger, config, scope, roles, state):
        result = SimpleNamespace(state=state, prepared=SimpleNamespace(scope=scope))
        model = public_model(result, metadata(), config)
        assert model['status'] == model['score']['status'] == 'conditional'
        assert model['completed_stages'] == state['card']['completed_stages']
        assert model['eligible_for_ranking']
        public_claims = {claim['id']: claim for claim in model['claims']}
        assert state['factor_handoff']
        for handed_off in state['factor_handoff']:
            assert public_claims[handed_off['claim_id']]['text'] == handed_off['neutral_text']
            assert public_claims[handed_off['claim_id']]['verification_text_source'] == 'factor_handoff'
        review = model['legal_reviews'][0]
        assert 'local_licensing_not_established' in review['gaps']
        assert 'The terms do not establish our regulatory status.' in review['gaps']
        assert len(review['clauses']) == (1 if coverage == 'insufficient' else 0)
        publish_snapshot(tmp_path, versioned_snapshot([model]))
        assert load_snapshot(tmp_path)['models'][0]['rank'] == 1
        with TestClient(create_app(data_dir=tmp_path, database_url='')) as client:
            response = client.get('/models/model-a?run=reassessment-a')
            assert response.status_code == 200
            assert 'Conditional assessment' in response.text
            assert 'Before the first paid customer obtain local legal review' in response.text

    run_case(ConditionalRoles(), conditional_packet(coverage), project)


@pytest.mark.parametrize('current_status,label', [('queued', 'update queued'), ('running', 'update in progress'),
                                                 ('technical_failed', 'update interrupted')])
def test_pending_cards_keep_previous_findings_inline_without_current_score(config, tmp_path, current_status, label):
    prior = public_model(result_fixture(), metadata(), config)
    prior['criteria'][0]['reason'] = 'Previous criterion finding retained for reference.'
    prior['sources'][0]['title'] = 'Previous source document'
    prior['claims'][0]['text'] = 'Previous claim passport retained for reference.'
    publish_snapshot(tmp_path, snapshot([prior]))
    previous_bytes = (tmp_path/'public'/'collection-a.json').read_bytes()
    pending = public_model(None, {**metadata(), 'base_model_id': prior['base_model_id'],
        'status': current_status, 'scoring_policy_version': '1.5.2', 'queue_position': 1}, config)
    value = versioned_snapshot([pending])
    publish_snapshot(tmp_path, value)
    with TestClient(create_app(data_dir=tmp_path, database_url='')) as client:
        detail = client.get('/models/model-a').text
        assert f'Previous assessment — {label}' in detail
        assert 'Original policy: legacy all-pass policy' in detail
        assert 'Previous criterion finding retained for reference.' in detail
        assert 'Previous source document' in detail
        assert 'Previous claim passport retained for reference.' in detail
        assert 'excluded from the current ranking and comparison' in detail
        assert 'No score yet' in detail
        assert detail.count('id="criteria"') == 1 and detail.count('id="sources"') == 1
        current_json = client.get('/runs/reassessment-a?format=json').json()['models'][0]
        assert current_json['score'] is None and current_json['criteria'] == [] and current_json['sources'] == []
        compare = client.get('/compare?run=reassessment-a&models=model-a&revision=reassessment-revision-a').text
        assert 'Previous source document' not in compare and '45–65' not in compare
        assert 'Previous assessment —' not in client.get('/runs/reassessment-a').text
        completed = public_model(conditional_result(), metadata(), config)
        completed['criteria'][0]['reason'] = 'Updated criterion finding.'
        value.update(revision_id='completed-revision', models=[completed])
        publish_snapshot(tmp_path, value)
        updated = client.get('/models/model-a').text
        assert 'Updated criterion finding.' in updated
        assert 'Previous assessment —' not in updated
        assert 'Previous criterion finding retained for reference.' not in updated
        assert 'Previous source document' not in updated
    assert (tmp_path/'public'/'collection-a.json').read_bytes() == previous_bytes


def test_prior_inline_assessment_must_match_identity_and_scenario(config, tmp_path):
    prior = public_model(result_fixture(), metadata(), config)
    publish_snapshot(tmp_path, snapshot([prior]))
    pending = public_model(None, {**metadata(), 'base_model_id': 'different-base', 'status': 'queued',
                                  'scoring_policy_version': '1.5.2'}, config)
    value = versioned_snapshot([pending])
    publish_snapshot(tmp_path, value)
    with TestClient(create_app(data_dir=tmp_path, database_url='')) as client:
        assert 'Previous assessment —' not in client.get('/models/model-a').text
        pending['base_model_id'] = prior['base_model_id']
        value['revision_id'] = 'another-scenario'
        value['scenario']['jurisdiction'] = 'Another jurisdiction'
        publish_snapshot(tmp_path, value)
        assert 'Previous assessment —' not in client.get('/models/model-a').text


def test_known_legal_document_details_keep_literal_values_and_evidence(config, tmp_path):
    result = conditional_result()
    document = result.state['card']['legal_reviews'][0]['documents'][0]
    values = {'version': 'Version 2026.11', 'effective_date': 'November 15, 2026',
              'supplier_legal_entity': 'Example Software Limited'}
    document.update(**values, metadata_evidence={
        field: dict(evidence_id='evidence-a', source_snapshot_id='snapshot-a',
                    excerpt=f'The document states {value}.', locator=f'Legal header: {field}',
                    private_audit_ref='PRIVATE-METADATA-AUDIT') for field, value in values.items()},
        metadata_unknown_reasons={'internal_private_field': 'PRIVATE-UNKNOWN-REASON'})
    model = public_model(result, metadata(), config)
    projected = model['legal_reviews'][0]['documents'][0]
    for field, value in values.items():
        assert projected[field] == value
        assert value in projected['metadata_evidence'][field]['excerpt']
        assert projected['metadata_evidence'][field]['source_id'] == 'snapshot-a'
    assert projected['metadata_unknown_reasons'] == {}
    assert 'PRIVATE-' not in json.dumps(model)
    publish_snapshot(tmp_path, versioned_snapshot([model]))
    with TestClient(create_app(data_dir=tmp_path, database_url='')) as client:
        detail = client.get('/models/model-a').text
        for value in values.values():
            assert value in detail
        assert 'Supplier legal entity' in detail and 'Evidence for document details' in detail
        assert 'Legal header: effective_date' in detail
        assert 'PRIVATE-' not in detail


def test_unknown_legal_document_details_show_reasons_without_invented_values(config, tmp_path):
    result = conditional_result()
    document = result.state['card']['legal_reviews'][0]['documents'][0]
    reasons = {'version': 'No version label appears in the read text.',
               'effective_date': 'The text states a revision date, but no effective date.',
               'supplier_legal_entity': 'The contracting entity is not identified in this document.'}
    document.update(version=None, effective_date=None, supplier_legal_entity=None,
                    metadata_evidence={}, metadata_unknown_reasons=reasons)
    model = public_model(result, metadata(), config)
    projected = model['legal_reviews'][0]['documents'][0]
    assert projected['metadata_unknown_reasons'] == reasons
    assert all(projected[field] is None for field in reasons)
    publish_snapshot(tmp_path, versioned_snapshot([model]))
    with TestClient(create_app(data_dir=tmp_path, database_url='')) as client:
        detail = client.get('/models/model-a').text
        for reason in reasons.values():
            assert reason in detail
        assert 'Evidence for document details' not in detail
        assert detail.count('Not established</span>') == 3


def test_public_metadata_rejects_known_values_without_matching_document_quotes(config):
    model = public_model(conditional_result(), metadata(), config)
    document = model['legal_reviews'][0]['documents'][0]
    document['version'] = 'v2'
    with pytest.raises(ValueError, match='requires evidence from that document'):
        Model.model_validate(model)
    document['metadata_evidence'] = {'version': {'source_id': 'another-document', 'excerpt': 'Version v2', 'locator': 'Header'}}
    with pytest.raises(ValueError, match='requires evidence from that document'):
        Model.model_validate(model)


def test_metadata_quote_preserves_long_legal_entity_literal(config):
    result = conditional_result()
    document = result.state['card']['legal_reviews'][0]['documents'][0]
    name = 'Example ' + 'International ' * 27 + 'Limited'
    quote = f'The supplier legal entity for this agreement is {name}.'
    assert 320 < len(quote) < 600
    document.update(supplier_legal_entity=name, metadata_evidence={'supplier_legal_entity': dict(
        source_snapshot_id='snapshot-a', evidence_id='evidence-a', excerpt=quote, locator='Parties')})
    projected = public_model(result, metadata(), config)['legal_reviews'][0]['documents'][0]
    assert projected['supplier_legal_entity'] == name
    assert projected['metadata_evidence']['supplier_legal_entity']['excerpt'] == quote


def test_each_document_shows_substantive_applicability_or_explicit_gap(config, tmp_path):
    result = conditional_result()
    review = result.state['card']['legal_reviews'][0]
    review['documents'][0].update(applicability_finding='Our proposed software role includes a separate customer data obligation.',
        applicability_evidence=dict(evidence_id='evidence-a', source_snapshot_id='snapshot-a',
            excerpt='Customers control their account data.', locator='Customer obligations', private_ref='PRIVATE-FINDING-AUDIT'),
        applicability_unknown_reason=None)
    review['documents'].append(dict(url='https://example.com/privacy', document_type='privacy', title='Privacy policy',
        disposition='inaccessible', version=None, effective_date=None, supplier_legal_entity=None,
        metadata_unknown_reasons={field:'document_not_read' for field in ('version','effective_date','supplier_legal_entity')},
        applicability_finding=None, applicability_evidence=None, applicability_unknown_reason='document_not_read'))
    model = public_model(result, metadata(), config)
    assert len(model['legal_reviews'][0]['documents']) == 2
    assert 'PRIVATE-' not in json.dumps(model)
    publish_snapshot(tmp_path, versioned_snapshot([model]))
    with TestClient(create_app(data_dir=tmp_path, database_url='')) as client:
        detail = client.get('/models/model-a').text
        assert 'Our proposed software role includes a separate customer data obligation.' in detail
        assert 'Customers control their account data.' in detail
        assert 'Evidence for this applicability finding' in detail
        assert 'not an approval of our legal status' in detail
        assert 'This document was not read.' in detail
        assert 'document_not_read' not in detail


def test_public_factor_passport_uses_actual_verified_handoff_and_labels_author(config, tmp_path):
    result = conditional_result()
    original = result.state['factors']['claims'][0]
    author_text = 'The factor deserves a strong rating because interviews are easy to arrange.'
    neutral_text = 'The proposed demand test includes interviews with potential customers before building.'
    original['neutral_text'] = author_text
    result.state['factor_handoff'] = [{**original, 'neutral_text':neutral_text,
                                       'private_coverage':'PRIVATE-COVERAGE'}]
    result.state['factor_handoff_policy'] = 'factor-neutral-projection-repair-1'
    result.state['factor_projection_audit'] = {'private_gate':'PRIVATE-AUDIT', 'source_units':['PRIVATE-SOURCE-UNITS']}
    model = public_model(result, metadata(), config)
    claim = next(claim for claim in model['claims'] if claim['id'] == 'plan-a')
    assert claim['text'] == neutral_text
    assert claim['author_text'] == author_text
    assert claim['verification_text_source'] == 'factor_handoff'
    assert claim['verification'] == 'not_fact_checkable'
    assert claim['assumptions'] == original['assumptions']
    serialized = json.dumps(model)
    assert 'factor_projection_audit' not in serialized and 'PRIVATE-' not in serialized
    assert 'factor-neutral-projection-repair-1' not in serialized
    publish_snapshot(tmp_path, versioned_snapshot([model]))
    with TestClient(create_app(data_dir=tmp_path, database_url='')) as client:
        detail = client.get('/models/model-a').text
        assert neutral_text in detail and 'Author’s original rationale' in detail
        assert author_text in detail and 'This original wording was not the verifier’s input.' in detail
        assert 'Author assumptions' in detail
        assert 'PRIVATE-' not in detail


def test_unverified_or_unrelated_factor_handoff_cannot_replace_public_claim(config):
    result = conditional_result()
    original = result.state['factors']['claims'][0]
    result.state['factor_handoff'] = [
        {**original, 'neutral_text':'UNVERIFIED-NEUTRAL-REWRITE'},
        {**result.state['critique']['claims'][0], 'neutral_text':'UNRELATED-CRITIC-REWRITE'}]
    result.state['factor_verification'] = {'items':[]}
    model = public_model(result, metadata(), config)
    claim = next(claim for claim in model['claims'] if claim['id'] == 'plan-a')
    assert claim['text'] == original['neutral_text'] and claim['verification'] is None
    assert 'verification_text_source' not in claim and 'author_text' not in claim
    assert 'UNVERIFIED-NEUTRAL-REWRITE' not in json.dumps(model)
    assert 'UNRELATED-CRITIC-REWRITE' not in json.dumps(model)


def test_legacy_factor_passport_shape_is_unchanged_without_recorded_handoff(config):
    model = public_model(result_fixture(), metadata(), config)
    assert all('verification_text_source' not in claim and 'author_text' not in claim for claim in model['claims'])
