"""Real checkpoints and immutable receipts for the new policy, with offline role fixtures."""
import asyncio
import hashlib
import os
from decimal import Decimal
from pathlib import Path

import pytest

from ai_builder.config import load_config
from ai_builder.pipeline import Document, execute_scope
from ai_builder.pg_execution import execution_pool
from ai_builder.pg_migrate import migrate
from test_pg_ledger import setup
from test_pipeline import FixtureRoles, packet

DSN = os.getenv('TEST_DATABASE_URL')
pytestmark = pytest.mark.skipif(not DSN, reason='Real PostgreSQL requires TEST_DATABASE_URL')


def conditional_packet(coverage='insufficient', execution_state='completed'):
    scope = packet()
    scope.neutral_model_description += '. We do not lend, insure or hold client funds.'
    text = 'Fixture provider supplies workflow software. Customers remain responsible for their activity.'
    document = Document(snapshot_id='terms-snapshot', evidence_id='terms', url='https://example.com/terms',
                        text=text, content_hash=hashlib.sha256(text.encode()).hexdigest(), policy_status='validated')
    readable = coverage not in {'not_found', 'unavailable'}
    if readable:
        scope.documents.append(document)
    law_text = 'Fixture public authority states that the described software-only service is permitted without a special licence.'
    scope.documents.append(Document(snapshot_id='normative-snapshot', evidence_id='normative',
        url='https://authority.example.gov/software', text=law_text,
        content_hash=hashlib.sha256(law_text.encode()).hexdigest(), policy_status='validated'))
    scope.legal_source_reviews = [dict(analogue_id='fixture-analogue', company_name='Fixture', product_name='Workflow',
        official_urls=['https://example.com'], execution_state=execution_state, coverage=coverage,
        search_actions=[dict(action='search', query='Fixture Terms', outcome='succeeded')],
        documents=[dict(evidence_id='terms', snapshot_id='terms-snapshot', url=document.url,
                        document_type='terms', disposition='read')] if readable else [],
        gaps=[dict(code='local_licensing_not_established')])]
    return scope


class ConditionalRoles(FixtureRoles):
    def __init__(self, *, unknowns=('F01', 'F03'), kinds=None, fails=(), bad_quote=False,
                 bad_factor=False, affected=None, post_status='insufficient', omit_condition_effect=False):
        super().__init__()
        self.unknowns, self.kinds, self.fails = set(unknowns), kinds or {}, set(fails)
        self.bad_quote, self.bad_factor = bad_quote, bad_factor
        self.affected, self.post_status = affected, post_status
        self.omit_condition_effect = omit_condition_effect

    def version(self):
        return 'conditional-fixtures-1'

    async def generate(self, role, request, schema, key):
        if role in {'research', 'applicability'}:
            self.calls.append((role, request, key))
        if role == 'research':
            result = dict(criteria=[dict(criterion_id=f'F{i:02}', state='completed',
                result='fail' if f'F{i:02}' in self.fails else 'unknown' if f'F{i:02}' in self.unknowns else 'pass',
                evidence_ids=['evidence'], reason='Offline fixture only', design_basis=[]) for i in range(1, 11)],
                legal_reviews=[])
            for source in request['legal_source_reviews']:
                readable = [item for item in source['documents'] if item.get('disposition', 'read') in {'read','duplicate'}]
                read = bool(readable)
                metadata = []
                for document in readable:
                    saved = next(item for item in request['documents'] if item['evidence_id'] == document['evidence_id'])
                    unknown = dict(value=None, excerpt=None, locator=None, unknown_reason='Not explicitly stated in this fixture document.')
                    metadata.append(dict(evidence_id=saved['evidence_id'], source_snapshot_id=saved['snapshot_id'],
                        version=dict(unknown), effective_date=dict(unknown), supplier_legal_entity=dict(unknown),
                        applicability=dict(value='The document describes the supplier software relationship; our local role needs separate review.',
                            excerpt=saved['text'].split('.')[0]+'.', locator='paragraph 1', unknown_reason=None)))
                result['legal_reviews'].append(dict(analogue_id=source['analogue_id'],
                    coverage='reviewed' if read else source['coverage'], supplier_role='Software supplier.',
                    our_role_applicability='Our proposed software role needs a separate local licensing check.',
                    clauses=[dict(evidence_id='terms', source_snapshot_id='terms-snapshot',
                        excerpt='Invented quotation.' if self.bad_quote else 'Fixture provider supplies workflow software.',
                        locator='paragraph 1', finding='The supplier states its software role.')] if read else [],
                    gaps=['The terms do not establish our regulatory status.'], document_metadata=metadata))
            return result
        if role == 'applicability':
            assert not {'research', 'criteria', 'eligibility_conditions', 'legal_reviews'} & set(request)
            items = []
            for rule in request['criteria_rules']:
                identifier = rule['id']
                status = ('supports_fail' if identifier in self.fails else
                          'insufficient' if identifier in self.unknowns else 'supports_pass')
                if 'claims' in request and identifier == self.affected:
                    status = self.post_status
                kind = self.kinds.get(identifier, 'evidence_gap') if status == 'insufficient' else None
                condition = None
                bases = []
                if kind == 'evidence_gap' and identifier in {'F01','F03','F05','F06','F07'}:
                    condition = dict(assumption='The specified software role can meet ordinary launch requirements.',
                        required_check='Before the first paid customer obtain local legal review of our exact role.',
                        impact_on_factors='Budget a bounded paid review and launch delay; stop if a licence is required.')
                    if identifier in {'F05','F06','F07'}:
                        bases = [dict(kind='proposed_design', assessment_scope_id='scope',
                                      excerpt='We do not lend, insure or hold client funds.')]
                normative = []
                if identifier in {'F01','F03'} and status == 'supports_pass':
                    normative = [dict(evidence_id='normative', source_snapshot_id='normative-snapshot',
                        excerpt='Fixture public authority states that the described software-only service is permitted without a special licence.',
                        locator='paragraph 1', finding='Applicable to the synthetic fixture software role.',
                        authority_type='official_guidance', authority_name='Fixture public authority')]
                items.append(dict(criterion_id=identifier, status=status, reason='Independent fixture review.',
                                  evidence_ids=['normative'] if normative else ['evidence'], normative_basis=normative,
                                  design_basis=bases, unknown_kind=kind, condition=condition))
            return dict(items=items)
        result = await super().generate(role, request, schema, key)
        if role in {'critic', 'repair', 'factors'}:
            for claim in result.claims:
                claim.method = 'model_judgment' if claim.kind == 'judgment' else 'primary_source'
                claim.confidence = 'low'
                claim.confidence_reason = 'Synthetic fixture, not real market research'
                if claim.kind == 'judgment':
                    claim.assumptions = ['A paid review costs within the stated range and completes in the planned time.']
                    if role == 'factors' and not self.omit_condition_effect:
                        claim.assumptions += [f"{c['condition_id']}: {c['assumption']} {c['required_check']} {c['impact_on_factors']}"
                                              for c in request['eligibility_conditions']]
            if role in {'critic', 'repair'} and self.affected:
                result.claims[0].affected_criterion_ids = [self.affected]
        if role == 'verifier' and self.bad_factor and any(c['claim_id'] == 'estimate' for c in request['claims']):
            result.items[0].status = 'insufficient'
        return result


def run_case(roles, scope=None, callback=None):
    async def check():
        await migrate(DSN)
        config = load_config(Path(__file__).parents[1]/'config/conditional-config.json')
        async with execution_pool(DSN) as pool:
            ledger, _, _ = await setup(pool, config)
            selected = scope or conditional_packet()
            result = await execute_scope(ledger, config, selected, roles)
            if callback:
                await callback(ledger, config, selected, roles, result)
            return result
    return asyncio.run(check())


@pytest.mark.parametrize('coverage', ['insufficient', 'not_found', 'unavailable'])
def test_completed_document_gaps_allow_full_conditional_pipeline_and_replay(coverage):
    async def replay(ledger, config, scope, roles, state):
        count = len(roles.calls)
        repeated = await execute_scope(ledger, config, scope, roles)
        assert repeated['card'] == state['card']
        assert len(roles.calls) == count
    roles = ConditionalRoles()
    state = run_case(roles, conditional_packet(coverage), replay)
    card = state['card']
    assert card['eligibility'] == card['score']['status'] == 'conditional'
    assert card['publication'] == 'ready' and card['analysis_status'] == 'complete'
    assert Decimal(card['score']['value']) == 50
    assert card['completed_stages'] == ['research','applicability','critic','neutrality','verifier',
                                        'reconciliation','factors','factor_validation','card_validation']
    assert {c['criterion_id'] for c in card['eligibility_conditions']} == {'F01','F03'}
    assert {c['criterion_id'] for c in state['criteria'] if c['result'] == 'unknown'} == {'F01','F03'}
    assert card['legal_reviews'][0]['source_coverage'] == coverage
    assert card['scoring_policy_version'] == '1.5.2'
    for role, request, _ in roles.calls:
        if role == 'verifier':
            assert not {'eligibility_conditions','legal_reviews','applicability_reviews'} & set(request)
            if any(c['claim_id'] == 'days' for c in request['claims']):
                assert all('Unverified scenario premises:' in c['neutral_text'] for c in request['claims'])
                assert all(card['eligibility_conditions'][0]['impact_on_factors'] in c['neutral_text'] for c in request['claims'])


@pytest.mark.parametrize('unknowns,kinds,fails,expected', [
    ((), {}, (), 'confirmed'),
    (('F05','F06','F07'), {}, (), 'conditional'),
    (('F04',), {}, (), 'unresolved'),
    (('F10',), {}, (), 'unresolved'),
    (('F03',), {'F03':'material_conflict'}, (), 'unresolved'),
    (('F03',), {'F03':'undefined_scenario'}, (), 'unresolved'),
    ((), {}, ('F01',), 'blocked'),
    ((), {}, ('F03',), 'blocked'),
    ((), {}, ('F10',), 'unresolved'),
])
def test_honest_gate_outcomes(unknowns, kinds, fails, expected):
    roles = ConditionalRoles(unknowns=unknowns, kinds=kinds, fails=fails)
    state = run_case(roles)
    assert state['card']['eligibility'] == expected
    if expected in {'confirmed','conditional'}:
        assert state['card']['score']['status'] == ('final' if expected == 'confirmed' else 'conditional')
    else:
        assert state['card']['score'] is None
        assert 'critic' not in [name for name, _, _ in roles.calls]
        assert state['card']['completed_stages'] == ['research','applicability']
        assert state['card']['legal_reviews']


@pytest.mark.parametrize('post_status,expected', [('insufficient','conditional'), ('supports_fail','blocked')])
def test_critic_recheck_reclassifies_without_author_verdict(post_status, expected):
    roles = ConditionalRoles(unknowns=(), affected='F03', post_status=post_status)
    state = run_case(roles)
    assert state['card']['eligibility'] == expected
    assert sum(role == 'applicability' for role, _, _ in roles.calls) == 2
    if expected == 'conditional':
        assert state['card']['score']['status'] == 'conditional'
        assert state['card']['eligibility_conditions'][0]['criterion_id'] == 'F03'
    else:
        assert state['card']['score'] is None
        assert 'factors' not in [name for name, _, _ in roles.calls]


def test_conditional_score_still_requires_verified_factor_premises():
    state = run_case(ConditionalRoles(bad_factor=True))
    assert state['eligibility'] == 'conditional'
    assert state['card']['score']['status'] == 'unavailable'
    assert state['card']['publication'] == 'draft'
    assert 'card_validation' not in state['card']['completed_stages']


@pytest.mark.parametrize('execution_state', ['pending','running','technical_failed'])
def test_unfinished_legal_research_cannot_become_an_evidence_gap(execution_state):
    roles = ConditionalRoles()
    with pytest.raises(ValueError, match='Legal source research is not completed'):
        run_case(roles, conditional_packet(execution_state=execution_state))
    assert roles.calls == []


def test_fabricated_legal_clause_is_rejected_with_bounded_corrections():
    roles = ConditionalRoles(bad_quote=True)
    with pytest.raises(ValueError, match='not an exact quotation'):
        run_case(roles)
    assert [role for role, _, _ in roles.calls] == ['research'] * 3


def test_conditional_factors_cannot_ignore_outstanding_conditions():
    roles = ConditionalRoles(omit_condition_effect=True)
    with pytest.raises(ValueError, match='account for each condition_id'):
        run_case(roles)
    assert sum(role == 'factors' for role, _, _ in roles.calls) == 3


@pytest.mark.parametrize('unknowns', [(), ('F01',)])
def test_researched_prohibition_disagreement_is_not_silently_waived(unknowns):
    class DisagreementRoles(ConditionalRoles):
        async def generate(self, role, request, schema, key):
            result = await super().generate(role, request, schema, key)
            if role == 'research':
                result['criteria'][0]['result'] = 'fail'
                result['criteria'][0]['reason'] = 'A supplied provision appears to prohibit the proposed activity.'
            return result
    roles = DisagreementRoles(unknowns=unknowns)
    state = run_case(roles)
    assert state['card']['eligibility'] == 'unresolved'
    assert state['card']['score'] is None and not state['card']['eligibility_conditions']
    review = next(r for r in state['applicability_reviews'] if r['criterion_id'] == 'F01')
    assert review['unknown_kind'] == 'material_conflict'
    assert 'critic' not in [name for name, _, _ in roles.calls]


@pytest.mark.parametrize('basis', ['none', 'terms', 'homepage'])
def test_vendor_contract_or_homepage_cannot_establish_legal_or_licensing_pass(basis):
    class TermsOnly(ConditionalRoles):
        async def generate(self, role, request, schema, key):
            result = await super().generate(role, request, schema, key)
            if role == 'applicability':
                for item in result['items']:
                    if item['criterion_id'] not in {'F01','F03'}:
                        continue
                    if basis == 'none':
                        item['evidence_ids'], item['normative_basis'] = ['terms'], []
                    else:
                        doc = next(d for d in request['documents'] if d['evidence_id'] == ('terms' if basis == 'terms' else 'evidence'))
                        item['evidence_ids'] = [doc['evidence_id']]
                        item['normative_basis'][0].update(evidence_id=doc['evidence_id'], source_snapshot_id=doc['snapshot_id'], excerpt=doc['text'])
            return result
    roles = TermsOnly(unknowns=())
    with pytest.raises(ValueError, match='normative basis|external authority'):
        run_case(roles)
    assert sum(role == 'applicability' for role, _, _ in roles.calls) == 3
    assert 'critic' not in [role for role, _, _ in roles.calls]


@pytest.mark.parametrize('raw_only', [False, True])
def test_condition_identifier_tokens_cannot_replace_operational_effect_passports(raw_only):
    class TokenOnly(ConditionalRoles):
        async def generate(self, role, request, schema, key):
            result = await super().generate(role, request, schema, key)
            if role == 'factors':
                for claim in result.claims:
                    if not raw_only or claim.claim_id in {'days','cost'}:
                        claim.assumptions = ['condition-F01 condition-F03']
            return result
    roles = TokenOnly()
    with pytest.raises(ValueError, match='literal assumption, check and effect'):
        run_case(roles)
    assert sum(role == 'factors' for role, _, _ in roles.calls) == 3


def metadata_packet():
    scope = conditional_packet()
    text = ('Fixture provider supplies workflow software. Contracting supplier: Fixture Workflow, Inc. '
            'Document version: v2026.11. Effective date: November 16, 2026. '
            'Copyright 2026. Retrieved on November 18, 2026.')
    terms = next(d for d in scope.documents if d.evidence_id == 'terms')
    terms.text, terms.content_hash = text, hashlib.sha256(text.encode()).hexdigest()
    privacy = 'Privacy notice. Personal data is processed for customer workflow instructions.'
    scope.documents.append(Document(evidence_id='privacy', snapshot_id='privacy-snapshot',
        url='https://example.com/privacy', text=privacy, content_hash=hashlib.sha256(privacy.encode()).hexdigest(),
        policy_status='validated'))
    scope.legal_source_reviews[0]['documents'].append(dict(evidence_id='privacy', snapshot_id='privacy-snapshot',
        url='https://example.com/privacy', document_type='privacy', disposition='read'))
    return scope


class MetadataRoles(ConditionalRoles):
    async def generate(self, role, request, schema, key):
        result = await super().generate(role, request, schema, key)
        if role == 'research':
            item = next(d for d in result['legal_reviews'][0]['document_metadata'] if d['evidence_id'] == 'terms')
            for field, value, quote in [
                ('version','v2026.11','Document version: v2026.11.'),
                ('effective_date','November 16, 2026','Effective date: November 16, 2026.'),
                ('supplier_legal_entity','Fixture Workflow, Inc.','Contracting supplier: Fixture Workflow, Inc.')]:
                item[field] = dict(value=value, excerpt=quote, locator='paragraph 1', unknown_reason=None)
        return result


def test_source_literal_legal_metadata_and_individual_document_review_survive_card_without_mutating_scope():
    scope = metadata_packet()
    original = scope.model_dump(mode='json')
    state = run_case(MetadataRoles(), scope)
    documents = {d['evidence_id']:d for d in state['card']['legal_reviews'][0]['documents']}
    assert scope.model_dump(mode='json') == original
    terms = documents['terms']
    assert terms['version'] == 'v2026.11' and terms['effective_date'] == 'November 16, 2026'
    assert terms['supplier_legal_entity'] == 'Fixture Workflow, Inc.'
    assert set(terms['metadata_evidence']) == {'version','effective_date','supplier_legal_entity'}
    assert terms['metadata_unknown_reasons'] == {} and terms['unknown_date_reason'] is None
    assert all(ref['source_snapshot_id'] == 'terms-snapshot' for ref in terms['metadata_evidence'].values())
    privacy = documents['privacy']
    assert privacy['version'] is privacy['effective_date'] is privacy['supplier_legal_entity'] is None
    assert set(privacy['metadata_unknown_reasons']) == {'version','effective_date','supplier_legal_entity'}
    assert privacy['applicability_finding'] and privacy['applicability_evidence']['source_snapshot_id'] == 'privacy-snapshot'


@pytest.mark.parametrize('damage', ['missing_document','duplicate_document','wrong_snapshot','invented_quote',
                                   'normalized_date','retrieval_date','copyright_date','brand_inference',
                                   'unknown_without_reason','unquoted_applicability'])
def test_legal_metadata_cannot_invent_values_or_skip_readable_documents(damage):
    class DamagedMetadata(MetadataRoles):
        async def generate(self, role, request, schema, key):
            result = await super().generate(role, request, schema, key)
            if role == 'research':
                items = result['legal_reviews'][0]['document_metadata']
                terms, privacy = items
                if damage == 'missing_document':
                    items.pop()
                elif damage == 'duplicate_document':
                    items[1] = dict(terms)
                elif damage == 'wrong_snapshot':
                    terms['source_snapshot_id'] = 'privacy-snapshot'
                elif damage == 'invented_quote':
                    terms['version']['excerpt'] = 'Invented version v2026.11.'
                elif damage == 'normalized_date':
                    terms['effective_date']['value'] = '2026-11-16'
                elif damage == 'retrieval_date':
                    terms['effective_date'].update(value='November 18, 2026', excerpt='Retrieved on November 18, 2026.')
                elif damage == 'copyright_date':
                    terms['effective_date'].update(value='2026', excerpt='Copyright 2026.')
                elif damage == 'brand_inference':
                    terms['supplier_legal_entity']['value'] = 'Fixture Workflow LLC'
                elif damage == 'unknown_without_reason':
                    privacy['version']['unknown_reason'] = None
                elif damage == 'unquoted_applicability':
                    privacy['applicability']['excerpt'] = None
            return result
    roles = DamagedMetadata()
    with pytest.raises(ValueError):
        run_case(roles, metadata_packet())
    assert [role for role, _, _ in roles.calls] == ['research'] * 3


def test_document_without_substantive_finding_requires_an_explicit_insufficiency_reason():
    class InsufficientPrivacy(MetadataRoles):
        async def generate(self, role, request, schema, key):
            result = await super().generate(role, request, schema, key)
            if role == 'research':
                result['legal_reviews'][0]['document_metadata'][1]['applicability'] = dict(
                    value=None, excerpt=None, locator=None,
                    unknown_reason='This privacy notice does not establish a requirement applicable to our operating role.')
            return result
    state = run_case(InsufficientPrivacy(), metadata_packet())
    privacy = state['card']['legal_reviews'][0]['documents'][1]
    assert privacy['applicability_finding'] is None and privacy['applicability_evidence'] is None
    assert 'does not establish' in privacy['applicability_unknown_reason']
    assert state['card']['score']['status'] == 'conditional'
