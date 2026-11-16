"""Real PG creation/binding; source and checkpoint validators have separate suites."""
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from psycopg.types.json import Jsonb

import ai_builder.complete_selected_scores as recovery
from ai_builder.collection import CollectionContext
from ai_builder.config import load_config
from ai_builder.evidence import ArtifactStore
from ai_builder.legal_sources import LegalAnalogue
from ai_builder.pg_execution import execution_pool
from ai_builder.pg_ledger import PostgresLedger, digest
from ai_builder.pg_migrate import migrate
from ai_builder.pipeline import prompt_bundle
from ai_builder.research_scope import ResearchContext
from ai_builder.source_research import SourceResearchPlan, ScopeSeed, RECOVERY_KAYAK_ID, RECOVERY_KAYAK_URLS
from ai_builder.store import uid
from ai_builder.upstream_revision import UPSTREAM_ROLES
from test_collection import context as fixture_context
from test_factor_revision import milestone_config
from test_public import metadata
from test_providers import pricing
from test_selected_score_recovery import current_bytes

DSN=os.getenv('TEST_DATABASE_URL')
pytestmark=pytest.mark.skipif(not DSN,reason='Real PostgreSQL requires TEST_DATABASE_URL')


def test_create_exact_two_runs_keeps_frozen_config_upstream_and_budget_membership(monkeypatch,tmp_path,config):
    verified=[]
    async def verified_parent(conn,**kwargs):
        # The actual source/checkpoint content is verified in factor-revision tests.
        # Here keep the real frozen compatibility boundary, then exercise SQL creation.
        context=kwargs['context']
        prior=await (await conn.execute('SELECT context FROM app.research_contexts WHERE run_id=%s',
                                       (context.parent_run_id,))).fetchone()
        old=ResearchContext.model_validate(prior['context'])
        assert old.pipeline_prefix==context.pipeline_prefix
        assert all(old.pipeline_prompts[role]==context.pipeline_prompts[role]
                   and old.role_settings[role]==context.role_settings[role] for role in UPSTREAM_ROLES)
        assert old.plan==context.plan
        verified.append(context.plan.seed.base_model_id)
        return None,SimpleNamespace(completed_through='reconcile')
    async def verified_sources(conn,plan,artifacts,study_id):
        assert plan.schema_version=='4' and plan.priority_primary_urls==RECOVERY_KAYAK_URLS
        return None,[]
    monkeypatch.setattr(recovery,'load_upstream_manifest',verified_parent)
    monkeypatch.setattr(recovery,'verify_recovery_sources',verified_sources)
    async def scenario():
        await migrate(DSN)
        old_cfg=load_config(Path(__file__).parents[1]/'config/conditional-config.json')
        cfg=milestone_config(old_cfg)
        prefix,prompts=prompt_bundle('1.3');old_prefix,old_prompts=prompt_bundle('1.2')
        settings={role:{'reasoning_effort':'medium','max_output_tokens':16384} for role in prompts}
        async with execution_pool(DSN) as pool:
            admin=PostgresLedger(pool);study=await admin.create_study('US','100')
            old_id,current_id=uid(),uid();old_coord=await admin.create_run(study,old_cfg);coord=await admin.create_run(study,cfg)
            old_runs={mid:await admin.create_run(study,old_cfg) for mid in recovery.TARGETS}
            parents={mid:await admin.create_run(study,cfg) for mid in recovery.TARGETS}
            ids=list(recovery.TARGETS+recovery.COMPLETED)+['retained-'+str(i) for i in range(15)]
            previous_context={'config':old_cfg.model_dump(mode='json')}
            raw_context=fixture_context(cfg).model_dump(mode='json')
            raw_context.update(config=cfg.model_dump(mode='json'),execution_version='collection-3',
                reassessment_of_collection_id=old_id,processing_order=list(recovery.TARGETS+recovery.COMPLETED),
                selected_model_ids=list(recovery.TARGETS+recovery.COMPLETED),role_settings=settings,
                pipeline_prefix=prefix,pipeline_prompts=prompts)
            current_context=CollectionContext.model_validate(raw_context)
            plans={mid:SourceResearchPlan(schema_version='3',seed=ScopeSeed(scope_id=uid(),variant_id=uid(),
                base_model_id=mid,neutral_model_description='Fixed proposed model.'),queries=(),legal_analogues=[
                LegalAnalogue(analogue_id='fixture',company_name='KAYAK',product_name='KAYAK',
                              official_urls=['https://www.kayak.com/'])]) for mid in recovery.TARGETS}
            originals=[];current=[]
            for pos,mid in enumerate(ids,1):
                plan=plans[mid].model_dump(mode='json') if mid in recovery.TARGETS else {'seed':{'base_model_id':mid}}
                originals.append(dict(model_id=mid,position=pos,candidate={'plan':plan,'metadata':metadata(mid)}))
                candidate={'plan':plan,'metadata':metadata(mid)}
                if mid in recovery.TARGETS:candidate['parent_run_id']=old_runs[mid]
                current.append(dict(model_id=mid,position=pos,candidate=candidate))
            async with pool.connection() as conn,conn.transaction():
                for cid,coordinator,ctx,members,runs in [(old_id,old_coord,previous_context,originals,old_runs),
                    (current_id,coord,current_context.model_dump(mode='json'),current,parents)]:
                    await conn.execute('''INSERT INTO app.collections(id,study_id,coordinator_run_id,context_hash,context,state,membership_hash,selected_count,frozen_at)
                        VALUES (%s,%s,%s,%s,%s,'partial',%s,20,clock_timestamp())''',
                        (cid,study,coordinator,digest(ctx),Jsonb(ctx),digest(members)))
                    for item in members:
                        await conn.execute('INSERT INTO app.collection_items(collection_id,model_id,position,candidate,run_id) VALUES (%s,%s,%s,%s,%s)',
                            (cid,item['model_id'],item['position'],Jsonb(item['candidate']),runs.get(item['model_id'])))
                for mid in recovery.TARGETS:
                    old=ResearchContext(plan=plans[mid],openai_pricing=pricing(),tavily_pricing=pricing('tavily'),
                        role_settings={role:settings[role] for role in old_prompts},purpose='eval',
                        pipeline_prefix=old_prefix,pipeline_prompts=old_prompts)
                    old_value=old.model_dump(mode='json')
                    await conn.execute('INSERT INTO app.research_contexts(run_id,context_hash,context) VALUES (%s,%s,%s)',
                                       (old_runs[mid],digest(old_value),Jsonb(old_value)))
                    manifest={'parent_run_id':old_runs[mid],'parent_collection_id':old_id}
                    research=ResearchContext(plan=plans[mid],openai_pricing=pricing(),tavily_pricing=pricing('tavily'),
                        role_settings=settings,purpose='eval',pipeline_prefix=prefix,pipeline_prompts=prompts,
                        parent_collection_id=old_id,parent_run_id=old_runs[mid],
                        upstream_manifest={'manifest_hash':digest(manifest),'manifest':manifest})
                    value=research.model_dump(mode='json')
                    await conn.execute('INSERT INTO app.research_contexts(run_id,context_hash,context) VALUES (%s,%s,%s)',
                                       (parents[mid],digest(value),Jsonb(value)))
                    await PostgresLedger.register_model_budget_family(conn,study_id=study,model_id=mid,
                                                                     ancestor_run_ids=[old_runs[mid],parents[mid]])
            value=json.loads(current_bytes(config));value['run_id']=current_id
            for model,identifier in zip(value['models'],ids):model['id']=identifier
            raw=json.dumps(value,indent=2).encode();folder=tmp_path/'public';folder.mkdir()
            (folder/f'{current_id}.json').write_bytes(raw);(folder/'latest.json').write_text(json.dumps({'run_id':current_id}))
            artifacts=ArtifactStore(tmp_path/'evidence')
            allowance=await recovery.create_recovery(pool,artifacts,tmp_path,current_id)
            assert await recovery.create_recovery(pool,artifacts,tmp_path,current_id)==allowance
            assert verified==list(recovery.TARGETS)
            assert (folder/f'{current_id}.json').read_bytes()==raw
            async with pool.connection() as conn:
                members=await (await conn.execute('SELECT * FROM app.recovery_runs WHERE allowance_id=%s',(allowance,))).fetchall()
                assert {m['model_id'] for m in members}==set(recovery.TARGETS) and len(members)==2
                for member in members:
                    assert member['parent_run_id']==parents[member['model_id']]
                    saved=await (await conn.execute('SELECT c.context,r.config_json,r.limit_micro FROM app.research_contexts c JOIN app.runs r ON r.id=c.run_id WHERE c.run_id=%s',
                                                  (member['run_id'],))).fetchone()
                    ctx=saved['context'];assert ctx['parent_run_id']==old_runs[member['model_id']]
                    assert ctx['recovery_allowance_id']==allowance
                    assert ctx['plan']['schema_version']==('4' if member['model_id']==RECOVERY_KAYAK_ID else '3')
                    assert saved['config_json']==cfg.model_dump(mode='json') and saved['limit_micro']==25_000_000
                    assert ctx['role_settings']==settings
                rows=await (await conn.execute('SELECT model_id,run_id FROM app.collection_items WHERE collection_id=%s',(current_id,))).fetchall()
                assert {r['model_id']:r['run_id'] for r in rows if r['model_id'] in recovery.TARGETS}==parents
                assert not await (await conn.execute('SELECT 1 FROM app.usage WHERE run_id=ANY(%s::text[])',
                    ([m['run_id'] for m in members],))).fetchone()
    asyncio.run(scenario())
