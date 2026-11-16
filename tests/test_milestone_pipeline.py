"""Offline role fixtures backed by real immutable PostgreSQL checkpoints."""
import asyncio
from copy import deepcopy
from decimal import Decimal
import os
from pathlib import Path

import pytest

from ai_builder.config import load_config
from ai_builder.pipeline import execute_scope, UpstreamInheritance, UPSTREAM_FIELDS, UPSTREAM_ORDER
from ai_builder.pg_execution import execution_pool
from ai_builder.pg_migrate import migrate
from test_conditional_pipeline import ConditionalRoles, conditional_packet
from test_pg_ledger import setup
from test_pipeline import claim

DSN = os.getenv('TEST_DATABASE_URL')
pytestmark = pytest.mark.skipif(not DSN,reason='Real PostgreSQL requires TEST_DATABASE_URL')
ROOT = Path(__file__).parents[1]


def milestone_output(request):
    def passport(identifier,text,quantity=None,events=(None,None),dependencies=()):
        return {**claim(identifier,'judgment',quantity),'neutral_text':text,
            'method':'model_judgment','confidence':'low','confidence_reason':'Offline bounded scenario fixture.',
            'assumptions':[text], 'input_claim_ids':list(dependencies),
            'start_event':events[0],'end_event':events[1]}
    claims,refs = [],[]
    for condition in request['eligibility_conditions']:
        identifier = 'factor-'+condition['condition_id']
        claims.append(passport(identifier,'This plan assumes the stated software role is allowed. '
            'Local legal review occurs before any customer pays. Its bounded cost and delay belong in the plan; '
            'an own licence requirement stops launch.'))
        refs.append(dict(condition_id=condition['condition_id'],claim_id=identifier))
    dependencies=[r['claim_id'] for r in refs]
    estimates=[('factor-preparation','Preparation under the stated assumptions takes 13.2 to 14.1 days.',dict(low='13.2',high='14.1'),('assessment_start','offer_ready')),
               ('factor-payment','The payment cycle under the stated assumptions takes 15 to 30 days.',dict(low=15,high=30),('offer_ready','first_payment_received')),
               ('factor-cost','The stated conditional test costs 100 to 500 dollars.',dict(low=100,high=500),(None,None))]
    for identifier,text,value,events in estimates:
        claims.append(passport(identifier,text,value,events,dependencies))
    for identifier,text in [('factor-access','The scenario assumes an available customer channel.'),
                            ('factor-paid','The scenario assumes a documented paid analogue.'),
                            ('factor-resources','The scenario assumes that the critical resources are conditionally available.'),
                            ('factor-plan','The proposed plan stops if the stated review identifies a required own licence.')]:
        claims.append(passport(identifier,text,dependencies=dependencies))
    return dict(factors=[dict(factor_id=fid,value=value,claim_ids=[identifier],rationale='AUTHOR-ONLY anchor selection for '+fid)
        for fid,value,identifier in [('S01',dict(low=2,high=2),'factor-access'),('S02',dict(low=3,high=3),'factor-paid'),
                                    ('S03',None,'factor-preparation'),('S04',None,'factor-payment'),('S05',dict(low=2,high=2),'factor-resources')]],
        claims=claims,preparation_days=dict(low='13.2',high='14.1'),preparation_claim_id='factor-preparation',
        payment_cycle_days=dict(low=15,high=30),payment_cycle_claim_id='factor-payment',cost=dict(low=100,high=500),
        cost_claim_id='factor-cost',resources='conditional',resources_claim_id='factor-resources',
        test_plan_claim_ids=['factor-plan'],condition_claims=refs,limitations=[])


class MilestoneRoles(ConditionalRoles):
    def __init__(self,defect=None,**kwargs):
        super().__init__(**kwargs)
        self.defect=defect

    def version(self):
        return 'milestone-fixtures-1'

    async def generate(self,role,request,schema,key):
        if role=='factor_neutrality':
            self.calls.append((role,request,key))
            assert 'AUTHOR-ONLY' not in str(request)
            assert {item['condition_id'] for item in request['condition_claims']}=={item['condition_id'] for item in request['reference_conditions']}
            return dict(status='reject' if self.defect=='semantic_rejection' else 'accept',
                        reason='Condition meaning missing.' if self.defect=='semantic_rejection' else
                               'Descriptive conditional operating rules, no reviewer commands or scores.')
        if role=='factors':
            self.calls.append((role,request,key))
            result=milestone_output(request)
            if self.defect=='unknown':
                result['preparation_days']=None;result['preparation_claim_id']=None
                result['factors'][2]['claim_ids']=[]
                result['claims']=[c for c in result['claims'] if c['claim_id']!='factor-preparation']
            elif self.defect=='double_count':
                next(c for c in result['claims'] if c['claim_id']=='factor-payment')['start_event']='assessment_start'
            elif self.defect=='author_timing_score':
                result['factors'][2]['value']=dict(low=4,high=4)
            elif self.defect=='omit_condition':
                result['condition_claims'].pop()
            elif self.defect=='drop_condition_dependency':
                next(c for c in result['claims'] if c['claim_id']=='factor-payment')['input_claim_ids']=[]
            elif self.defect=='hidden_assumption':
                next(c for c in result['claims'] if c['claim_id']=='factor-payment')['assumptions']=['An unstated guaranteed partner completes all work immediately.']
            elif self.defect=='wrong_quantity':
                result['preparation_days']['high']='20'
            return result
        result=await super().generate(role,request,schema,key)
        if role=='verifier':
            assert 'AUTHOR-ONLY' not in str(request) and 'rationale' not in str(request)
        return result


def run_milestone_case(roles=None,callback=None,scope=None):
    async def scenario():
        await migrate(DSN)
        config=load_config(ROOT/'config/milestone-config.json')
        async with execution_pool(DSN) as pool:
            ledger,_,_=await setup(pool,config)
            selected=scope or conditional_packet()
            selected_roles=roles or MilestoneRoles()
            state=await execute_scope(ledger,config,selected,selected_roles)
            if callback:
                await callback(ledger,config,selected,selected_roles,state)
            return state
    return asyncio.run(scenario())


def test_milestones_are_separate_and_total_is_calculated_without_author_rationale_in_handoff():
    async def replay(ledger,config,scope,roles,state):
        count=len(roles.calls)
        assert (await execute_scope(ledger,config,scope,roles,resume=True))['card']==state['card']
        assert len(roles.calls)==count
    roles=MilestoneRoles(); state=run_milestone_case(roles,replay)
    assert state['card']['score']['status']=='conditional'
    assert Decimal(state['card']['score']['value'])==Decimal('66.25')
    assert state['card']['timing']['preparation']['estimate']==dict(low='13.2',high='14.1')
    assert state['card']['timing']['preparation']['days']==dict(low='14',high='15')
    assert state['card']['timing']['payment_cycle']['days']==dict(low='15',high='30')
    assert state['factors']['days']==state['card']['timing']['total']['days']==dict(low='29',high='45')
    total=next(c for c in state['factors']['claims'] if c['claim_id']=='factor-time-to-first-payment')
    assert total['kind']=='calculation' and total['input_claim_ids']==['factor-preparation','factor-payment']
    assert state['card']['inherited_stages']==[]
    assert sum(role=='factor_neutrality' for role,_,_ in roles.calls)==1
    assert sum(role=='neutrality' for role,_,_ in roles.calls)==1  # Critic's unchanged rubric.
    factor_verifier=next(p for role,p,_ in roles.calls if role=='verifier' and any(c['claim_id']=='factor-plan' for c in p['claims']))
    assert factor_verifier['claims']==state['factor_handoff']
    assert 'rationale' not in str(factor_verifier) and 'AUTHOR-ONLY' not in str(factor_verifier)
    assert 'reference_conditions' not in factor_verifier and 'condition_claims' not in factor_verifier


def test_unknown_milestone_is_not_filled_from_horizon_or_other_estimate():
    state=run_milestone_case(MilestoneRoles('unknown'))
    assert state['card']['score']['status']=='unavailable'
    assert state['factors']['days'] is None and state['card']['timing']['total']['claim_id'] is None
    assert not any(c['claim_id']=='factor-time-to-first-payment' for c in state['factors']['claims'])


def test_semantic_condition_rejection_stops_without_a_lexical_repair_chain():
    roles=MilestoneRoles('semantic_rejection');state=run_milestone_case(roles)
    assert state['card']['score']['status']=='unavailable'
    assert 'factor_neutrality_unresolved' in state['card']['publication_blockers']
    assert 'factor_verification' not in state
    assert not any(role=='repair' for role,_,_ in roles.calls)
    assert sum(role=='verifier' for role,_,_ in roles.calls)==1  # Inherited/earlier critic stage only.


@pytest.mark.parametrize('defect',['double_count','author_timing_score','omit_condition','drop_condition_dependency','hidden_assumption','wrong_quantity'])
def test_invalid_milestone_contract_cannot_reach_factor_verifier(defect):
    roles=MilestoneRoles(defect)
    with pytest.raises(ValueError):
        run_milestone_case(roles)
    assert sum(role=='factors' for role,_,_ in roles.calls)==3
    assert not any(role=='factor_neutrality' for role,_,_ in roles.calls)


def test_inherited_upstream_starts_at_factors_and_creates_no_copied_receipts():
    async def scenario():
        await migrate(DSN)
        old=load_config(ROOT/'config/conditional-config.json');new=load_config(ROOT/'config/milestone-config.json')
        async with execution_pool(DSN) as pool:
            parent,_,_=await setup(pool,old)
            scope=conditional_packet()
            prior=await execute_scope(parent,old,scope,ConditionalRoles())
            allowed=set().union(*UPSTREAM_FIELDS.values())
            inherited=UpstreamInheritance(manifest_hash='fixture-verified-manifest',parent_run_id=parent.run_id,
                completed_through='reconcile',state={key:deepcopy(value) for key,value in prior.items() if key in allowed})
            child,_,_=await setup(pool,new)
            roles=MilestoneRoles()
            state=await execute_scope(child,new,scope,roles,inheritance=inherited)
            assert [role for role,_,_ in roles.calls]==['factors','factor_neutrality','verifier']
            assert state['card']['inherited_stages']==['research','applicability','critic','neutrality','verifier','reconciliation']
            assert state['research']==prior['research'] and state['handoff']==prior['handoff']
            async with pool.connection() as conn:
                rows=await (await conn.execute('SELECT node_name FROM app.node_receipts WHERE run_id=%s',(child.run_id,))).fetchall()
            assert {r['node_name'] for r in rows}=={'factors','factor_neutrality','verifier','card'}
            before=len(roles.calls)
            assert (await execute_scope(child,new,scope,roles,resume=True,inheritance=inherited))['card']==state['card']
            assert len(roles.calls)==before
    asyncio.run(scenario())
