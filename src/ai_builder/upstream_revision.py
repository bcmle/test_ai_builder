"""Explicit, verified references to completed upstream work; never copied receipts.

The manifest is frozen in the new collection candidate and research context.
Every load checks the original checkpoint, source artifacts and receipt hashes.
"""
from __future__ import annotations

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from .config import Configuration
from .pg_ledger import digest
from .pipeline import ModelRoles
from .source_research import SourceResearchPlan, _materialize, _result_rows
from .store import Conflict


PREFIX_FIELDS = (
    ('research', ('research', 'legal_reviews')),
    ('applicability', ('criteria', 'eligibility', 'eligibility_conditions', 'applicability_reviews')),
    ('critic', ('critique',)),
    ('neutrality', ('neutral',)),
    ('verifier', ('verification',)),
    ('reconcile', ('reconciliation',)),
)
UPSTREAM_ROLES = {'research', 'applicability', 'critic', 'repair', 'neutrality', 'verifier'}


def upstream_prefix(values):
    """A failed next node contributes nothing. Never inherit Factors or a card."""
    state, through = {}, None
    for node, names in PREFIX_FIELDS:
        if not all(name in values for name in names):
            break
        state.update({name: values[name] for name in names})
        through = node
        if node == 'neutrality' and values['neutral']:
            if 'handoff' not in values:
                raise Conflict('Accepted neutral checkpoint is missing its handoff')
            state['handoff'] = values['handoff']
        # Eligibility may have been updated by neutrality/reconciliation in the
        # same checkpoint. Existing later fields still identify completed work.
        if node == 'neutrality' and not state['neutral']:
            break
    return through, state


async def _parent_inputs(conn, parent_run_id, config, new_context, artifacts):
    from .research_scope import ResearchContext, _collection_parent
    parent = await _collection_parent(conn, parent_run_id)
    if await (await conn.execute('''SELECT 1 FROM app.run_leases WHERE run_id=%s
        AND expires_at>clock_timestamp()''', (parent_run_id,))).fetchone():
        raise Conflict('Upstream run still has an active owner')
    if await (await conn.execute('''SELECT 1 FROM app.usage WHERE run_id=%s
        AND state IN ('reserved','unknown')''', (parent_run_id,))).fetchone():
        raise Conflict('Upstream run has unresolved external usage')
    saved = await (await conn.execute('SELECT * FROM app.research_contexts WHERE run_id=%s',
                                     (parent_run_id,))).fetchone()
    if not saved or digest(saved['context']) != saved['context_hash']:
        raise Conflict('Upstream research context is missing or changed')
    old = ResearchContext.model_validate(saved['context'])
    old_config = Configuration.model_validate(parent['parent_config'])
    if (old_config.schema_version != '1.2' or config.schema_version != '1.3'
            or old_config.scenario != config.scenario or old_config.market != config.market
            or old_config.model != config.model or old_config.criteria != config.criteria
            or old.openai_pricing != new_context.openai_pricing
            or old.tavily_pricing != new_context.tavily_pricing or old.purpose != new_context.purpose
            or old.pipeline_prefix != new_context.pipeline_prefix
            or any(old.pipeline_prompts.get(role) != new_context.pipeline_prompts.get(role)
                   or old.role_settings.get(role) != new_context.role_settings.get(role) for role in UPSTREAM_ROLES)):
        raise Conflict('Upstream inputs or role contracts are incompatible with the factor revision')
    source = await (await conn.execute('SELECT * FROM app.source_research WHERE run_id=%s',
                                      (parent_run_id,))).fetchall()
    if (len(source) != 1 or source[0]['state'] != 'frozen'
            or source[0]['plan'] != old.plan.model_dump(mode='json')
            or source[0]['plan_hash'] != digest(source[0]['plan'])
            or source[0]['packet_hash'] != digest({'scope':source[0]['packet'], 'gaps':source[0]['gaps']})):
        raise Conflict('Upstream source packet is not complete and intact')
    source = source[0]
    prepared = _materialize(old.plan, await _result_rows(conn, parent_run_id, old.plan.seed.scope_id), artifacts)
    if (prepared.scope.model_dump(mode='json') != source['packet']
            or [g.model_dump(mode='json') for g in prepared.gaps] != source['gaps']
            or prepared.scope.base_model_id != parent['model_id']):
        raise Conflict('Upstream source artifacts do not match the frozen packet')
    graph_input = await (await conn.execute('SELECT * FROM app.scope_inputs WHERE run_id=%s',
                                           (parent_run_id,))).fetchone()
    thread = await (await conn.execute('SELECT * FROM app.graph_threads WHERE run_id=%s AND scope_id=%s',
                                      (parent_run_id, old.plan.seed.scope_id))).fetchone()
    roles = ModelRoles(None, old.openai_pricing, '', old.role_settings, purpose=old.purpose,
                       prompt_prefix=old.pipeline_prefix, prompts=old.pipeline_prompts)
    expected_input = digest({'scope':source['packet'], 'config':old_config.digest(), 'roles':roles.version()})
    if graph_input is not None and (graph_input['scope'] != source['packet']
            or graph_input['role_settings'] != old.role_settings
            or graph_input['pricing'] != old.openai_pricing.model_dump(mode='json')
            or graph_input['purpose'] != old.purpose):
        raise Conflict('Upstream graph input differs from frozen sources and roles')
    if thread and (not graph_input or thread['input_hash'] != expected_input):
        raise Conflict('Upstream thread is bound to different inputs')
    return parent, saved, old, source, graph_input, thread, prepared


async def freeze_upstream_manifest(conn, *, parent_run_id, config, context, artifacts):
    parent, saved, old, source, graph_input, thread, prepared = await _parent_inputs(
        conn, parent_run_id, config, context, artifacts)
    checkpoint = None
    if thread:
        checkpoint = await AsyncPostgresSaver(conn).aget_tuple({'configurable':{'thread_id':thread['thread_id']}})
    values = checkpoint.checkpoint['channel_values'] if checkpoint else {}
    through, state = upstream_prefix(values)
    receipts = await (await conn.execute('''SELECT invocation_key,scope_id,node_name,input_hash,result
        FROM app.node_receipts WHERE run_id=%s AND node_name=ANY(%s::text[]) ORDER BY invocation_key''',
        (parent_run_id, sorted(UPSTREAM_ROLES)))).fetchall()
    # Actual author/verifier outputs must have accepted receipts, not merely a checkpoint field.
    for field, roles in [('research', {'research'}), ('critique', {'critic','repair'}), ('verification', {'verifier'})]:
        if field in state and not any(r['node_name'] in roles and r['result'] == state[field] for r in receipts):
            raise Conflict('Upstream state has no matching accepted role receipt: '+field)
    value = dict(schema_version='factor-upstream-1', parent_run_id=parent_run_id,
        parent_collection_id=parent['id'], parent_config_hash=parent['parent_config_hash'],
        parent_context_hash=saved['context_hash'], source_packet_hash=source['packet_hash'],
        scope_id=prepared.scope.scope_id, scope_input_hash=digest(graph_input) if graph_input else None,
        thread_id=thread['thread_id'] if thread else None,
        graph_input_hash=thread['input_hash'] if thread else None,
        checkpoint_id=checkpoint.checkpoint['id'] if checkpoint else None,
        completed_through=through, state=state,
        receipts=[{key:r[key] for key in ('invocation_key','scope_id','node_name','input_hash')}
                  | {'result_hash':digest(r['result'])} for r in receipts])
    return {'manifest_hash':digest(value), 'manifest':value}


async def load_upstream_manifest(conn, *, envelope, config, context, artifacts):
    from .pipeline import UpstreamInheritance
    if set(envelope) != {'manifest_hash','manifest'} or digest(envelope['manifest']) != envelope['manifest_hash']:
        raise Conflict('Upstream manifest integrity mismatch')
    value = envelope['manifest']
    if value['parent_run_id'] != context.parent_run_id or value['parent_collection_id'] != context.parent_collection_id:
        raise Conflict('Upstream manifest references a different parent')
    parent, saved, old, source, graph_input, thread, prepared = await _parent_inputs(
        conn, value['parent_run_id'], config, context, artifacts)
    if (old.plan != context.plan or parent['parent_config_hash'] != value['parent_config_hash']
            or saved['context_hash'] != value['parent_context_hash']
            or source['packet_hash'] != value['source_packet_hash']
            or (digest(graph_input) if graph_input else None) != value['scope_input_hash']
            or (thread['thread_id'] if thread else None) != value['thread_id']
            or (thread['input_hash'] if thread else None) != value['graph_input_hash']):
        raise Conflict('Upstream manifest no longer matches its original inputs')
    values = {}
    if value['checkpoint_id']:
        checkpoint = await AsyncPostgresSaver(conn).aget_tuple({'configurable':{
            'thread_id':value['thread_id'], 'checkpoint_id':value['checkpoint_id']}})
        if not checkpoint:
            raise Conflict('Referenced upstream checkpoint is missing')
        values = checkpoint.checkpoint['channel_values']
    if upstream_prefix(values) != (value['completed_through'], value['state']):
        raise Conflict('Referenced upstream checkpoint changed')
    for ref in value['receipts']:
        receipt = await (await conn.execute('''SELECT scope_id,node_name,input_hash,result FROM app.node_receipts
            WHERE run_id=%s AND invocation_key=%s''', (value['parent_run_id'],ref['invocation_key']))).fetchone()
        if not receipt or any(receipt[key] != ref[key] for key in ('scope_id','node_name','input_hash')) or digest(receipt['result']) != ref['result_hash']:
            raise Conflict('Referenced upstream receipt is missing or changed')
    inheritance = UpstreamInheritance(manifest_hash=envelope['manifest_hash'], parent_run_id=value['parent_run_id'],
                                      completed_through=value['completed_through'], state=value['state'])
    return prepared, inheritance
