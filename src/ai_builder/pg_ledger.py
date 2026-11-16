"""PostgreSQL money ledger; all mutating accounting paths lock study, then run.

API authorization is outside this module. A worker ledger is bound to a run and
lease; late settlement is allowed for that run without reviving a dead worker.
"""
import hashlib
import json
from contextlib import asynccontextmanager

from psycopg.types.json import Jsonb
from psycopg.pq import TransactionStatus

from .store import BudgetExceeded, Conflict, micro, uid
from .pg_execution import assert_lease


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def metrics_checked(metrics):
    allowed = {'input_tokens','output_tokens','cached_input_tokens','reasoning_tokens','credits',
               'billable_units','duration_ms','status','error_kind','provider_request_id'}
    if set(metrics) - allowed:
        raise ValueError('Unrecognized usage metric')
    for key in ('input_tokens','output_tokens','cached_input_tokens','reasoning_tokens','duration_ms'):
        v = metrics.get(key)
        if v is not None and (type(v) is not int or v < 0):
            raise ValueError('Invalid usage counter')
    for subset, total in [('cached_input_tokens','input_tokens'),('reasoning_tokens','output_tokens')]:
        if metrics.get(subset) is not None and metrics.get(total) is not None and metrics[subset] > metrics[total]:
            raise ValueError('Usage subset exceeds total')
    # Reject NaN and non-JSON objects before opening a transaction.
    json.dumps(metrics, allow_nan=False)


class PostgresLedger:
    def __init__(self, pool, run_id=None, token=None):
        self.pool, self.run_id, self.token = pool, run_id, token

    def _scope(self, run_id):
        if self.run_id is not None and self.run_id != run_id:
            raise Conflict('Wrong run scope')

    @asynccontextmanager
    async def locked(self, run_id, fenced=False):
        self._scope(run_id)
        async with self.pool.connection() as conn, conn.transaction():
            row = await (await conn.execute('SELECT study_id FROM app.runs WHERE id=%s', (run_id,))).fetchone()
            if row is None:
                raise KeyError(run_id)
            study = await (await conn.execute('SELECT * FROM app.studies WHERE id=%s FOR UPDATE', (row['study_id'],))).fetchone()
            run = await (await conn.execute('SELECT * FROM app.runs WHERE id=%s FOR UPDATE', (run_id,))).fetchone()
            if fenced:
                if not self.token:
                    raise Conflict('Worker lease required')
                await assert_lease(conn, run_id, self.token)
            yield conn, study, run

    async def create_study(self, market, budget):
        identifier = uid()
        async with self.pool.connection() as conn:
            await conn.execute('INSERT INTO app.studies VALUES (%s,%s,%s)', (identifier,market,micro(budget)))
        return identifier

    async def create_run(self, study_id, config):
        identifier = uid()
        async with self.pool.connection() as conn, conn.transaction():
            study = await (await conn.execute('SELECT market FROM app.studies WHERE id=%s FOR UPDATE', (study_id,))).fetchone()
            if not study or study['market'] != config.market:
                raise ValueError('Run must match study market')
            await conn.execute('''INSERT INTO app.runs(id,study_id,config_json,config_hash,limit_micro,status,
                graph_version,state_schema_version,coordinator_thread_id)
                VALUES (%s,%s,%s,%s,%s,'selecting','scope-1','1',%s)''',
                (identifier,study_id,Jsonb(config.model_dump(mode='json')),config.digest(),micro(config.limits.run_cost_usd),uid()))
            await conn.execute('''INSERT INTO app.run_commands(id,run_id,kind,expected_control_version,idempotency_key,payload)
                VALUES (%s,%s,'start',1,'start',%s)''', (uid(),identifier,Jsonb({})))
        return identifier

    @staticmethod
    async def _known_model_runs(conn, study_id, model_id):
        """Do not silently omit an orphaned or newly introduced model run."""
        rows = await (await conn.execute('''SELECT i.run_id FROM app.collection_items i
            JOIN app.collections c ON c.id=i.collection_id
            WHERE c.study_id=%s AND i.model_id=%s AND i.run_id IS NOT NULL
            UNION SELECT r.id FROM app.runs r JOIN app.research_contexts x ON x.run_id=r.id
            WHERE r.study_id=%s AND x.context->'plan'->'seed'->>'base_model_id'=%s''',
            (study_id,model_id,study_id,model_id))).fetchall()
        return {row['run_id'] for row in rows}

    @staticmethod
    async def _model_run_proof(conn, study_id, model_id, run_id):
        row = await (await conn.execute('''SELECT r.study_id,r.limit_micro,i.collection_id,i.candidate,
            c.context,c.context_hash,c.membership_hash,x.context AS research_context,x.context_hash AS research_hash
            FROM app.runs r JOIN app.collection_items i ON i.run_id=r.id
            JOIN app.collections c ON c.id=i.collection_id
            LEFT JOIN app.research_contexts x ON x.run_id=r.id
            WHERE r.id=%s AND i.model_id=%s AND c.study_id=%s''', (run_id,model_id,study_id))).fetchone()
        if not row or row['study_id'] != study_id or not row['research_context']:
            raise Conflict('Model budget ancestry requires an unambiguous saved collection run')
        if (digest(row['context']) != row['context_hash']
                or digest(row['research_context']) != row['research_hash']):
            raise Conflict('Model budget ancestry context hash mismatch')
        members = await (await conn.execute('''SELECT model_id,position,candidate FROM app.collection_items
            WHERE collection_id=%s ORDER BY position''', (row['collection_id'],))).fetchall()
        if not row['membership_hash'] or digest(members) != row['membership_hash']:
            raise Conflict('Model budget ancestry membership hash mismatch')
        research = row['research_context']
        if (row['candidate'].get('plan',{}).get('seed',{}).get('base_model_id') != model_id
                or research.get('plan',{}).get('seed',{}).get('base_model_id') != model_id):
            raise Conflict('Model budget ancestry model identity mismatch')
        parent_collection = row['context'].get('reassessment_of_collection_id')
        parent_run = row['candidate'].get('parent_run_id')
        if (research.get('parent_run_id') != parent_run
                or research.get('parent_collection_id') != parent_collection
                or (parent_run is None) != (parent_collection is None)):
            raise Conflict('Model budget ancestry has inconsistent direct parent links')
        if parent_run is not None:
            parent = await (await conn.execute('''SELECT i.run_id,c.study_id FROM app.collection_items i
                JOIN app.collections c ON c.id=i.collection_id WHERE i.collection_id=%s AND i.model_id=%s''',
                (parent_collection,model_id))).fetchone()
            if not parent or parent['study_id'] != study_id or parent['run_id'] != parent_run:
                raise Conflict('Model budget ancestor does not match the declared parent collection')
        return {'run_id':run_id,'parent_run_id':parent_run,'run_limit_micro':row['limit_micro']}

    @staticmethod
    async def register_model_budget_family(conn, *, study_id, model_id, ancestor_run_ids, limit='25'):
        """Freeze verified historical membership in the caller's transaction.

        The caller must use a transaction; this helper locks study before any
        accounting work and performs no commit, lease acquisition or external I/O.
        """
        if conn.info.transaction_status != TransactionStatus.INTRANS:
            raise Conflict('Model budget registration requires an explicit transaction')
        cap = micro(limit)
        if not model_id or not 0 < cap <= micro('25'):
            raise ValueError('Model revision family limit must be positive and at most $25')
        ancestors = list(ancestor_run_ids)
        if not ancestors or len(ancestors) != len(set(ancestors)):
            raise Conflict('Model budget ancestry must list each historical run exactly once')
        study = await (await conn.execute('SELECT id FROM app.studies WHERE id=%s FOR UPDATE', (study_id,))).fetchone()
        if not study:
            raise KeyError(study_id)
        if set(ancestors) != await PostgresLedger._known_model_runs(conn,study_id,model_id):
            raise Conflict('Model budget ancestry must include all known model runs')
        proofs = {identifier:await PostgresLedger._model_run_proof(conn,study_id,model_id,identifier)
                  for identifier in ancestors}
        roots = [p for p in proofs.values() if p['parent_run_id'] is None]
        if len(roots) != 1:
            raise Conflict('Model budget ancestry must have exactly one original run')
        for identifier in proofs:
            seen, current = set(), identifier
            while current is not None:
                if current not in proofs or current in seen:
                    raise Conflict('Model budget ancestry is missing a parent or contains a cycle')
                seen.add(current)
                current = proofs[current]['parent_run_id']
        effective = min(cap, *(p['run_limit_micro'] for p in proofs.values()))
        existing = await (await conn.execute('''SELECT * FROM app.model_budget_families
            WHERE study_id=%s AND model_id=%s''', (study_id,model_id))).fetchone()
        if existing:
            members = await (await conn.execute('''SELECT * FROM app.model_budget_members
                WHERE study_id=%s AND model_id=%s''', (study_id,model_id))).fetchall()
            if {m['run_id'] for m in members} != set(ancestors):
                raise Conflict('New model revisions must bind to their existing immutable family')
            old_cap = min(existing['limit_micro'], *(m['run_limit_micro'] for m in members))
            if cap < old_cap:
                raise Conflict('A lower requested family cap requires a matching lower run limit')
            return {'study_id':study_id,'model_id':model_id,'limit_micro':min(effective,old_cap)}
        ordered = sorted(ancestors)
        await conn.execute('''INSERT INTO app.model_budget_families
            (study_id,model_id,limit_micro,ancestor_run_ids,ancestry_hash) VALUES (%s,%s,%s,%s,%s)''',
            (study_id,model_id,effective,Jsonb(ordered),digest(ordered)))
        for proof in proofs.values():
            await conn.execute('''INSERT INTO app.model_budget_members
                (run_id,study_id,model_id,parent_run_id,run_limit_micro) VALUES (%s,%s,%s,%s,%s)''',
                (proof['run_id'],study_id,model_id,proof['parent_run_id'],proof['run_limit_micro']))
        await PostgresLedger.event(conn,roots[0]['run_id'],'model_budget_family_registered',
            {'model_id':model_id,'ancestor_run_ids':ordered,'limit_micro':effective,'ancestry_hash':digest(ordered)})
        return {'study_id':study_id,'model_id':model_id,'limit_micro':effective}

    @staticmethod
    async def bind_model_budget_run(conn, *, study_id, model_id, run_id, parent_run_id):
        """Attach a new selected child atomically with collection run assignment."""
        if conn.info.transaction_status != TransactionStatus.INTRANS:
            raise Conflict('Model budget binding requires an explicit transaction')
        await conn.execute('SELECT id FROM app.studies WHERE id=%s FOR UPDATE', (study_id,))
        family = await (await conn.execute('''SELECT * FROM app.model_budget_families
            WHERE study_id=%s AND model_id=%s''', (study_id,model_id))).fetchone()
        if not family:
            raise Conflict('Model budget family must be registered before creating a revision run')
        proof = await PostgresLedger._model_run_proof(conn,study_id,model_id,run_id)
        if not parent_run_id or proof['parent_run_id'] != parent_run_id:
            raise Conflict('New model budget member must preserve its explicit parent run')
        members = await (await conn.execute('''SELECT * FROM app.model_budget_members
            WHERE study_id=%s AND model_id=%s''', (study_id,model_id))).fetchall()
        by_id = {m['run_id']:m for m in members}
        if parent_run_id not in by_id:
            raise Conflict('Model budget parent is not in the same immutable family')
        if await PostgresLedger._known_model_runs(conn,study_id,model_id) != set(by_id) | {run_id}:
            raise Conflict('Model budget ancestry contains an unbound or omitted run')
        if run_id in by_id:
            if by_id[run_id]['parent_run_id'] != parent_run_id:
                raise Conflict('Model budget run membership is immutable')
            return
        await conn.execute('''INSERT INTO app.model_budget_members
            (run_id,study_id,model_id,parent_run_id,run_limit_micro) VALUES (%s,%s,%s,%s,%s)''',
            (run_id,study_id,model_id,parent_run_id,proof['run_limit_micro']))
        await PostgresLedger.event(conn,run_id,'model_budget_run_bound',
            {'model_id':model_id,'parent_run_id':parent_run_id,'family_limit_micro':family['limit_micro']})

    @staticmethod
    async def _model_budget(conn, run, used=None, *, required=True):
        member = await (await conn.execute('''SELECT f.* FROM app.model_budget_members m
            JOIN app.model_budget_families f USING(study_id,model_id) WHERE m.run_id=%s''', (run['id'],))).fetchone()
        if not member:
            if required and run['config_json'].get('schema_version') == '1.3':
                raise Conflict('Model revision has no verified cumulative budget family')
            if required:
                known = await (await conn.execute('''SELECT 1 FROM app.collection_items i
                    JOIN app.model_budget_families f ON f.model_id=i.model_id AND f.study_id=%s
                    WHERE i.run_id=%s''', (run['study_id'],run['id']))).fetchone()
                if known:
                    raise Conflict('Historical model run is missing from its cumulative budget family')
            return None
        rows = await (await conn.execute('''SELECT m.*,r.limit_micro FROM app.model_budget_members m
            JOIN app.runs r ON r.id=m.run_id WHERE m.study_id=%s AND m.model_id=%s''',
            (member['study_id'],member['model_id']))).fetchall()
        ids = {row['run_id'] for row in rows}
        if member['study_id'] != run['study_id'] or ids != await PostgresLedger._known_model_runs(conn,run['study_id'],member['model_id']):
            raise Conflict('Cumulative model budget ancestry is incomplete')
        limit = min(member['limit_micro'], *(min(r['run_limit_micro'],r['limit_micro']) for r in rows))
        if used is None:
            totals = await (await conn.execute('''SELECT COALESCE(sum(CASE WHEN state='settled' THEN calculated_micro
                WHEN state IN ('reserved','unknown') THEN upper_micro ELSE 0 END),0) AS used
                FROM app.usage WHERE run_id=ANY(%s)''', (list(ids),))).fetchone()
            total = totals['used']
        else:
            total = sum(used.get(identifier,0) for identifier in ids)
        return {'model_id':member['model_id'],'limit_micro':limit,'accounted_micro':int(total),
                'remaining_micro':int(limit-total),'run_ids':sorted(ids)}

    async def register_pricing(self, body):
        identifier = digest(body)
        async with self.pool.connection() as conn:
            await conn.execute('INSERT INTO app.pricing(id,body) VALUES (%s,%s) ON CONFLICT DO NOTHING', (identifier,Jsonb(body)))
        return identifier

    @staticmethod
    async def event(conn, run_id, kind, payload):
        await conn.execute('INSERT INTO app.events(run_id,kind,payload) VALUES (%s,%s,%s)', (run_id,kind,Jsonb(payload)))

    async def reserve(self, *, run_id, request_id, logical_request_id, provider, model,
                      endpoint, agent, purpose, pricing_id, upper):
        if purpose not in {'development','eval','live'}:
            raise ValueError('Invalid purpose')
        policy = {('openai','responses'):'provider_requests', ('tavily','search'):'search_requests',
                  ('tavily','extract'):'document_fetches'}
        if (provider,endpoint) not in policy:
            raise ValueError('Unknown accounting endpoint')
        amount = micro(upper)
        blocked = False
        async with self.locked(run_id, fenced=True) as (conn, study, run):
            old = await (await conn.execute('SELECT * FROM app.usage WHERE request_id=%s', (request_id,))).fetchone()
            expected = dict(run_id=run_id,logical_request_id=logical_request_id,provider=provider,model=model,
                            endpoint=endpoint,agent=agent,purpose=purpose,pricing_id=pricing_id,upper_micro=amount)
            if old:
                if any(old[k] != v for k,v in expected.items()):
                    raise Conflict('Request identity reused with different input')
                await self._model_budget(conn,run)
                return False
            if run['status'] not in {'selecting','queued','running'}:
                raise Conflict('Run not accepting requests')
            cur = await conn.execute('''SELECT u.run_id, sum(CASE WHEN state='settled' THEN calculated_micro
                WHEN state IN ('reserved','unknown') THEN upper_micro ELSE 0 END) AS used
                FROM app.usage u JOIN app.runs r ON r.id=u.run_id WHERE r.study_id=%s GROUP BY u.run_id''', (study['id'],))
            used = {r['run_id']:r['used'] for r in await cur.fetchall()}
            family = await self._model_budget(conn,run,used)
            count = await (await conn.execute('SELECT count(*) AS n FROM app.usage WHERE run_id=%s AND provider=%s AND endpoint=%s',
                                              (run_id,provider,endpoint))).fetchone()
            request_count = count['n']
            if (provider, endpoint) == ('tavily', 'extract'):
                fetched = await (await conn.execute(
                    'SELECT count(*) AS n FROM app.source_fetch_attempts WHERE run_id=%s',
                    (run_id,))).fetchone()
                request_count += fetched['n']
            blocked = (sum(used.values())+amount > study['limit_micro'] or used.get(run_id,0)+amount > run['limit_micro']
                       or (family is not None and family['accounted_micro']+amount > family['limit_micro'])
                       or request_count >= run['config_json']['limits'][policy[provider,endpoint]]
                       or await self.active_ms(conn, run) >= run['config_json']['limits']['active_seconds']*1000)
            if blocked:
                await conn.execute("UPDATE app.runs SET status='paused',control_version=control_version+1 WHERE id=%s", (run_id,))
                await self.event(conn,run_id,'budget_paused',{'request_id':request_id})
            else:
                await conn.execute('''INSERT INTO app.usage(request_id,logical_request_id,run_id,provider,model,
                    endpoint,agent,purpose,pricing_id,upper_micro,state) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'reserved')''',
                    (request_id,logical_request_id,run_id,provider,model,endpoint,agent,purpose,pricing_id,amount))
                await self.event(conn,run_id,'budget_reserved',{'request_id':request_id,'upper_micro':amount})
        if blocked:
            raise BudgetExceeded('Run/study/model-family budget or request cap exhausted')
        return True

    @asynccontextmanager
    async def request_locked(self, request_id):
        async with self.pool.connection() as conn:
            row = await (await conn.execute('SELECT run_id FROM app.usage WHERE request_id=%s', (request_id,))).fetchone()
        if not row:
            raise KeyError(request_id)
        async with self.locked(row['run_id']) as (conn, study, run):
            usage = await (await conn.execute('SELECT * FROM app.usage WHERE request_id=%s FOR UPDATE', (request_id,))).fetchone()
            yield conn, usage

    async def settle(self, request_id, calculated, metrics, billed=None):
        metrics_checked(metrics)
        amount, bill = micro(calculated), None if billed is None else micro(billed)
        async with self.request_locked(request_id) as (conn,row):
            if row['state'] == 'released':
                raise Conflict('Released usage needs explicit reconciliation')
            if (row['state'],row['calculated_micro'],row['billed_micro'],row['metrics']) == ('settled',amount,bill,metrics):
                return
            await conn.execute("UPDATE app.usage SET state='settled',calculated_micro=%s,billed_micro=%s,metrics=%s,updated_at=clock_timestamp() WHERE request_id=%s",
                               (amount,bill,Jsonb(metrics),request_id))
            await self.event(conn,row['run_id'],'usage_corrected' if row['state']=='settled' else 'usage_settled',
                             {'request_id':request_id,'previous_micro':row['calculated_micro'],'calculated_micro':amount})
            if amount > row['upper_micro']:
                await conn.execute("UPDATE app.runs SET status='paused',control_version=control_version+1 WHERE id=%s", (row['run_id'],))
                await self.event(conn,row['run_id'],'reservation_underestimated',{'request_id':request_id})

    async def mark_unknown(self, request_id, error_kind='unknown_usage'):
        async with self.request_locked(request_id) as (conn,row):
            if row['state']=='reserved':
                await conn.execute("UPDATE app.usage SET state='unknown',metrics=%s,updated_at=clock_timestamp() WHERE request_id=%s",
                                   (Jsonb({'status':'unknown','error_kind':error_kind}),request_id))
                await self.event(conn,row['run_id'],'usage_unknown',{'request_id':request_id,'error_kind':error_kind})

    async def release(self, request_id, reason):
        if not reason.strip():
            raise ValueError('Non-billing evidence required')
        async with self.request_locked(request_id) as (conn,row):
            if row['state']=='settled':
                raise Conflict('Cannot release settled usage')
            if row['state']=='released':
                return
            await conn.execute("UPDATE app.usage SET state='released',updated_at=clock_timestamp() WHERE request_id=%s", (request_id,))
            await self.event(conn,row['run_id'],'usage_released',{'request_id':request_id,'reason':reason})

    async def pause_run(self, run_id, reason):
        async with self.locked(run_id) as (conn,_,run):
            if run['status'] in {'selecting','queued','running'}:
                await conn.execute("UPDATE app.runs SET status='paused',control_version=control_version+1 WHERE id=%s", (run_id,))
                await self.event(conn,run_id,'run_paused',{'reason':reason})

    async def summary(self, run_id):
        self._scope(run_id)
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            run = await (await conn.execute('SELECT * FROM app.runs WHERE id=%s',(run_id,))).fetchone()
            if not run:
                raise KeyError(run_id)
            rows = await (await conn.execute('SELECT * FROM app.usage WHERE run_id=%s ORDER BY created_at,request_id',(run_id,))).fetchall()
            fetches = await (await conn.execute(
                'SELECT count(*) AS n FROM app.source_fetch_attempts WHERE run_id=%s', (run_id,))).fetchone()
            family = await self._model_budget(conn,run,required=False)
        totals = dict(calculated_micro=0,reserved_micro=0,unknown_micro=0,billed_micro=0,billed_unknown_count=0)
        for row in rows:
            if row['state']=='settled':
                totals['calculated_micro'] += row['calculated_micro']
            elif row['state'] in {'reserved','unknown'}:
                totals[row['state']+'_micro'] += row['upper_micro']
            if row['billed_micro'] is not None:
                totals['billed_micro'] += row['billed_micro']
            elif row['state']!='released':
                totals['billed_unknown_count'] += 1
        used = sum(totals[k] for k in ['calculated_micro','reserved_micro','unknown_micro'])
        result = dict(run_id=run_id,limit_micro=run['limit_micro'],remaining_micro=run['limit_micro']-used,
                    requests=rows, controlled_document_attempts=fetches['n'],
                    document_fetches_used=fetches['n']+sum(r['provider']=='tavily' and r['endpoint']=='extract' for r in rows),
                    **totals)
        if family is not None:
            result['model_budget_family'] = family
        return result

    async def begin_operation(self, key, input_hash):
        budget_blocked = False
        async with self.locked(self.run_id, fenced=True) as (conn,study,run):
            row = await (await conn.execute('SELECT * FROM app.provider_operations WHERE run_id=%s AND operation_key=%s', (self.run_id,key))).fetchone()
            if row:
                if row['input_hash'] != input_hash:
                    raise Conflict('Operation key input mismatch')
                if row['state']=='completed':
                    return row['response']
                # Commit the pause before surfacing the uncertain external operation.
                await conn.execute("UPDATE app.runs SET status='paused',control_version=control_version+1 WHERE id=%s", (self.run_id,))
                await self.event(conn,self.run_id,'uncertain_operation',{'operation_key':key})
            else:
                if run['status'] not in {'selecting','queued','running'}:
                    raise Conflict('Run paused')
                # Also fence unmetered input-count operations before their HTTP
                # request; a new revision may never start with unknown ancestry.
                family = await self._model_budget(conn,run)
                if family is not None:
                    cost = await (await conn.execute('''SELECT COALESCE(sum(CASE WHEN u.state='settled' THEN u.calculated_micro
                        WHEN u.state IN ('reserved','unknown') THEN u.upper_micro ELSE 0 END),0) AS used
                        FROM app.usage u JOIN app.runs r ON r.id=u.run_id WHERE r.study_id=%s''', (study['id'],))).fetchone()
                    budget_blocked = family['remaining_micro'] <= 0 or cost['used'] >= study['limit_micro']
                if budget_blocked:
                    await conn.execute("UPDATE app.runs SET status='paused',control_version=control_version+1 WHERE id=%s", (self.run_id,))
                    await self.event(conn,self.run_id,'budget_paused',{'operation_key':key,'stage':'before_external_operation'})
                else:
                    await conn.execute("INSERT INTO app.provider_operations VALUES (%s,%s,%s,'running',NULL)",(self.run_id,key,input_hash))
                    return None
        if budget_blocked:
            raise BudgetExceeded('Study/model-family budget exhausted before external operation')
        raise Conflict('Unfinished external operation: explicit review required before retry')

    async def finish_operation(self, key, response):
        async with self.locked(self.run_id, fenced=True) as (conn,_,__):
            result = await conn.execute("UPDATE app.provider_operations SET state='completed',response=%s WHERE run_id=%s AND operation_key=%s AND state='running'", (Jsonb(response),self.run_id,key))
            if result.rowcount != 1:
                raise Conflict('Operation not active')

    async def input_count_audit(self, key, status, metadata, *, response=None):
        """Record a count request separately from generation usage and money.

        A successful count and its audit commit together before generation may
        begin. Failed/interrupted counts remain running for explicit review.
        """
        if status not in {'started', 'completed', 'failed'} or (response is not None) != (status == 'completed'):
            raise ValueError('Invalid input count audit transition')
        async with self.locked(self.run_id, fenced=True) as (conn,_,__):
            if response is not None:
                updated = await conn.execute("""UPDATE app.provider_operations SET state='completed',response=%s
                    WHERE run_id=%s AND operation_key=%s AND state='running'""", (Jsonb(response),self.run_id,key))
                if updated.rowcount != 1:
                    raise Conflict('Input count operation not active')
            await self.event(conn,self.run_id,'input_token_count_'+status,
                             {**metadata,'operation_key':key,'cost':None,'billing':'not_reported'})

    async def receipt(self, scope, node, key, input_hash, result=None):
        async with self.locked(self.run_id, fenced=True) as (conn,_,__):
            row = await (await conn.execute('SELECT * FROM app.node_receipts WHERE run_id=%s AND invocation_key=%s',(self.run_id,key))).fetchone()
            if row:
                if (row['scope_id'],row['node_name'],row['input_hash']) != (scope,node,input_hash):
                    raise Conflict('Receipt input mismatch')
                if result is not None and result != row['result']:
                    raise Conflict('Immutable receipt mismatch')
                return row['result']
            if result is not None:
                await conn.execute('INSERT INTO app.node_receipts(run_id,invocation_key,scope_id,node_name,input_hash,result) VALUES (%s,%s,%s,%s,%s,%s)',
                                   (self.run_id,key,scope,node,input_hash,Jsonb(result)))
                await self.event(conn,self.run_id,'node_succeeded',{'scope_id':scope,'node':node,'invocation_key':key})
            return result

    async def bind_thread(self, scope, input_hash, graph_version='scope-1'):
        async with self.locked(self.run_id, fenced=True) as (conn,_,run):
            if run['graph_version'] != graph_version:
                raise Conflict('Incompatible graph version')
            row = await (await conn.execute('SELECT * FROM app.graph_threads WHERE run_id=%s AND scope_id=%s',(self.run_id,scope))).fetchone()
            if row:
                if (row['input_hash'],row['graph_version']) != (input_hash,graph_version):
                    raise Conflict('Changed scope input requires a new run')
                return row['thread_id']
            thread = uid()
            await conn.execute('INSERT INTO app.graph_threads VALUES (%s,%s,%s,%s,%s)',(thread,self.run_id,scope,graph_version,input_hash))
            return thread

    @staticmethod
    async def active_ms(conn, run):
        row = await (await conn.execute('''SELECT active_elapsed_ms + CASE WHEN active_started_at IS NULL THEN 0
            ELSE GREATEST(0,EXTRACT(EPOCH FROM clock_timestamp()-active_started_at)*1000)::bigint END AS elapsed
            FROM app.runs WHERE id=%s''',(run['id'],))).fetchone()
        return row['elapsed']

    async def start_clock(self):
        async with self.locked(self.run_id, fenced=True) as (conn,_,run):
            if run['status'] not in {'selecting','queued','running'}:
                raise Conflict('Run is paused; owner action required')
            elapsed = await self.active_ms(conn, run)
            limit = run['config_json']['limits']['active_seconds']*1000
            if elapsed >= limit:
                raise BudgetExceeded('Active time exhausted')
            await conn.execute("UPDATE app.runs SET status='running',active_elapsed_ms=%s,active_started_at=clock_timestamp() WHERE id=%s",(elapsed,self.run_id))
            return (limit-elapsed)/1000

    async def stop_clock(self, status):
        if status not in {'partial','completed','paused'}:
            raise ValueError('Invalid final status')
        async with self.locked(self.run_id, fenced=True) as (conn,_,run):
            elapsed = await self.active_ms(conn,run)
            # Never undo a budget or authentication pause.
            if run['status']=='paused':
                status='paused'
            await conn.execute('UPDATE app.runs SET status=%s,active_elapsed_ms=%s,active_started_at=NULL WHERE id=%s',
                               (status,elapsed,self.run_id))
