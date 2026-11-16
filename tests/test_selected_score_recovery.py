"""Narrow source admission and byte-preserving two-card publication contracts."""
from copy import deepcopy
import json

import pytest

from ai_builder.complete_selected_scores import (TARGETS,COMPLETED,_json_spans,model_payload_hashes,
    replace_target,_untouched)
from ai_builder.legal_sources import LegalAnalogue
from ai_builder.public import public_model
from ai_builder.research_scope import ResearchContext,freeze_context
from ai_builder.source_research import (RECOVERY_KAYAK_ID,RECOVERY_KAYAK_URLS,SourceResearchPlan,
    ScopeSeed,_require_recovery_document)
from ai_builder.store import Conflict
from test_public import result_fixture,metadata,snapshot
from test_legal_sources import legal_plan


def recovery_plan(**changes):
    value=dict(schema_version='4',seed=ScopeSeed(scope_id='new-scope',variant_id='new-variant',
        base_model_id=RECOVERY_KAYAK_ID,neutral_model_description='Referral-only travel comparison'),
        queries=(),max_new_reads=3,priority_primary_urls=RECOVERY_KAYAK_URLS,recovery_from_run_id='old-source-run',
        legal_analogues=(LegalAnalogue(analogue_id='kayak',company_name='KAYAK',product_name='KAYAK',
                                      official_urls=('https://www.kayak.com/',)),))
    value.update(changes)
    return SourceResearchPlan(**value)


def test_source_recovery_version_is_bounded_and_legacy_serialization_is_unchanged():
    old=legal_plan()
    assert 'recovery_from_run_id' not in old.model_dump(mode='json')
    assert SourceResearchPlan.model_validate_json(old.model_dump_json())==old
    plan=recovery_plan()
    assert plan.schema_version=='4' and plan.max_new_reads==3 and not plan.queries
    assert SourceResearchPlan.model_validate_json(plan.model_dump_json())==plan


@pytest.mark.parametrize('change',[
    {'priority_primary_urls':RECOVERY_KAYAK_URLS+('https://example.com',)},
    {'priority_primary_urls':tuple(reversed(RECOVERY_KAYAK_URLS))},
    {'max_new_reads':4},{'recovery_from_run_id':None},{'reassessed_from_run_id':'other'},
    {'queries':({'query':'another search','max_results':1},)},
])
def test_source_recovery_cannot_expand_reads_queries_or_ancestry(change):
    with pytest.raises(ValueError): recovery_plan(**change)


def test_retained_old_doc_cannot_trigger_paid_research_without_readable_new_lead():
    plan=recovery_plan()
    retained=dict(query_definition={'retained_from_run_id':'old-source-run'},snapshot_state='succeeded',
                  disposition='read',text_ref='old-body')
    failed=dict(query_definition={'kind':'legal_official_urls'},snapshot_state='failed',disposition='inaccessible',text_ref=None)
    with pytest.raises(Conflict,match='no readable new'):
        _require_recovery_document(plan,[retained,failed])
    new={**failed,'snapshot_state':'succeeded','disposition':'read','text_ref':'new-body'}
    _require_recovery_document(plan,[retained,new])
    with pytest.raises(Conflict):
        _require_recovery_document(plan,[retained,{**new,'snapshot_state':'unsupported'}])


def current_bytes(config):
    ids=list(TARGETS+COMPLETED)+['untouched-'+str(i) for i in range(15)]
    models=[]
    for index,identifier in enumerate(ids):
        model=public_model(result_fixture(),metadata(identifier),config)
        model['title']='Travel / UTF-8 café '+str(index)
        model['rank']=index+1
        models.append(model)
    value=snapshot(models)
    # Noncanonical whitespace and escaping prove that unchanged payloads are not serialized again.
    return json.dumps(value,ensure_ascii=True,indent=2).encode()


def test_each_publication_preserves_every_other_model_byte_for_byte_and_all_original_ranks(config):
    raw=current_bytes(config); before=model_payload_hashes(raw)
    for identifier in TARGETS:
        source=json.loads(raw)
        model=deepcopy(next(m for m in source['models'] if m['id']==identifier))
        model['title']='Recovered '+identifier; model['rank']=None
        updated=replace_target(raw,model,'recovery-'+identifier,'2026-11-16T15:00:00Z')
        after=model_payload_hashes(updated)
        assert all(after[mid]==model_payload_hashes(raw)[mid] for mid in after if mid!=identifier)
        raw=updated
    assert all(model_payload_hashes(raw)[mid]==old for mid,old in before.items() if mid not in TARGETS)
    assert all(m['rank'] is not None for m in json.loads(raw)['models'] if m['id'] not in TARGETS)


def test_recovery_cannot_publish_third_model_or_hide_changed_untouched_payload(config):
    raw=current_bytes(config); model=json.loads(raw)['models'][2]
    with pytest.raises(Conflict,match='Only an existing recovery target'):
        replace_target(raw,model,'new','2026-11-16T15:00:00Z')
    plan={'baseline_model_hashes':model_payload_hashes(raw)}
    changed=json.loads(raw);changed['models'][2]['title']='Unexpected edit'
    with pytest.raises(Conflict,match='non-target'):
        _untouched(plan,json.dumps(changed).encode())


def test_json_spans_rejects_duplicate_models_and_trailing_content(config):
    raw=current_bytes(config)
    with pytest.raises(Conflict): _json_spans(raw+b'{}')
    value=json.loads(raw);value['models'][1]=value['models'][0]
    with pytest.raises(Conflict,match='Duplicate public model'):_json_spans(json.dumps(value).encode())


# Real PostgreSQL publication replay: no provider/model fixture is invoked here.
import asyncio
import os
from pathlib import Path
from psycopg.types.json import Jsonb
from ai_builder.complete_selected_scores import _publish_result,sha
from ai_builder.pg_execution import acquire_lease,execution_pool
from ai_builder.pg_ledger import PostgresLedger,digest
from ai_builder.pg_migrate import migrate
from ai_builder.store import uid

DSN=os.getenv('TEST_DATABASE_URL')


@pytest.mark.skipif(not DSN,reason='Real PostgreSQL requires TEST_DATABASE_URL')
def test_both_publications_replay_without_overwriting_later_revision_or_original_run_ids(config,tmp_path):
    async def scenario():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            admin=PostgresLedger(pool);study=await admin.create_study('US','100')
            coordinator=await admin.create_run(study,config); collection=uid()
            parents={mid:await admin.create_run(study,config) for mid in TARGETS}
            new_runs={mid:await admin.create_run(study,config) for mid in TARGETS}
            raw=current_bytes(config);value=json.loads(raw);value['run_id']=collection
            raw=json.dumps(value,ensure_ascii=True,indent=2).encode()
            public_dir=tmp_path/'public';public_dir.mkdir()
            path=public_dir/f'{collection}.json';path.write_bytes(raw)
            (public_dir/'latest.json').write_text(json.dumps({'run_id':collection}))
            frozen_context={'fixture':'unchanged'}
            members=[dict(model_id=m['id'],position=i+1,candidate={'metadata':metadata(m['id'])})
                     for i,m in enumerate(value['models'])]
            async with pool.connection() as conn,conn.transaction():
                await conn.execute('''INSERT INTO app.collections
                    (id,study_id,coordinator_run_id,context_hash,context,state,membership_hash,selected_count,frozen_at)
                    VALUES (%s,%s,%s,%s,%s,'partial',%s,20,clock_timestamp())''',
                    (collection,study,coordinator,digest(frozen_context),Jsonb(frozen_context),digest(members)))
                for item in members:
                    await conn.execute('''INSERT INTO app.collection_items(collection_id,model_id,position,candidate,run_id,public_model)
                        VALUES (%s,%s,%s,%s,%s,%s)''',(collection,item['model_id'],item['position'],Jsonb(item['candidate']),
                            parents.get(item['model_id']),Jsonb(next(m for m in value['models'] if m['id']==item['model_id']))))
            plan=dict(policy='selected-score-recovery-1',allowance_id=uid(),collection_id=collection,study_id=study,
                coordinator_run_id=coordinator,context_hash=digest(frozen_context),membership_hash=digest(members),
                baseline_snapshot=raw.decode(),baseline_snapshot_hash=sha(raw),baseline_model_hashes=model_payload_hashes(raw),
                parent_members=[dict(model_id=i['model_id'],run_id=parents.get(i['model_id']),candidate_hash=digest(i['candidate']))
                                for i in members])
            targets=[dict(model_id=mid,run_id=new_runs[mid],metadata=metadata(mid),config=config.model_dump(mode='json')) for mid in TARGETS]
            ledger=PostgresLedger(pool,coordinator,await acquire_lease(pool,coordinator,'publication-fixture'))
            result=result_fixture()
            for target in targets: assert await _publish_result(ledger,plan,target,result,tmp_path)
            final=path.read_bytes()
            for target in targets: assert await _publish_result(ledger,plan,target,result,tmp_path)
            assert path.read_bytes()==final
            assert (public_dir/'history'/collection/'revision-a.json').read_bytes()==raw
            assert all(model_payload_hashes(final)[mid]==old for mid,old in plan['baseline_model_hashes'].items() if mid not in TARGETS)
            async with pool.connection() as conn:
                rows=await (await conn.execute('SELECT model_id,run_id,candidate FROM app.collection_items WHERE collection_id=%s ORDER BY position',
                                               (collection,))).fetchall()
                assert [dict(model_id=r['model_id'],run_id=r['run_id'],candidate_hash=digest(r['candidate'])) for r in rows]==plan['parent_members']
                count=await (await conn.execute("SELECT count(*) AS n FROM app.events WHERE run_id=%s AND kind='selected_recovery_publication'",(coordinator,))).fetchone()
                assert count['n']==2
            # A second study worker must prevent even replay/publication.
            worker=await acquire_lease(pool,new_runs[TARGETS[0]],'unrelated-worker')
            with pytest.raises(Conflict,match='Another study worker'):
                await _publish_result(ledger,plan,targets[0],result,tmp_path)
            assert path.read_bytes()==final
    asyncio.run(scenario())

