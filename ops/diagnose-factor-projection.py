#!/usr/bin/env python3
"""Read-only diagnosis of persisted neutral-projection candidates. No provider calls."""
import argparse
from collections import Counter
from difflib import SequenceMatcher
import json
import os
import re
import sys

import psycopg
from psycopg.rows import dict_row
from pydantic import ValidationError
from ai_builder.domain import (
    FactorNeutralProjection, HandoffClaim, validate_factor_projection,
    _protected_premises, _quantities, _negative_anchors, _content_tokens,
    _operational_tokens, _MODAL_TERMS,
)


def safe(value, limit=1200):
    value = str(value)
    value = re.sub(r'\bsk-[A-Za-z0-9_-]+', '[REDACTED_KEY]', value)
    value = re.sub(r'(?i)bearer\s+\S+', 'Bearer [REDACTED]', value)
    value = re.sub(r'(https?://)[^/@\s]+:[^/@\s]+@', r'\1[REDACTED]@', value)
    return value if len(value) <= limit else value[:limit] + ' [truncated]'


def bounded(value, key='', depth=0):
    """Bound arbitrary saved identifiers/error paths as well as claim text."""
    if depth > 10:
        return '[depth truncated]'
    if isinstance(value,str):
        limit = 160 if key.endswith('_id') or key in {'operation_key','collection','path'} else 1200
        return safe(value,limit)
    if isinstance(value,dict):
        return {safe(k,80):bounded(v,str(k),depth+1) for k,v in list(value.items())[:40]}
    if isinstance(value,list):
        return [bounded(v,key,depth+1) for v in value[:40]]
    return value


def render_report(report, compact=False):
    if compact:
        keys = ('read_only','collection','position','run_id','state','error_code','candidates',
                'input_event_id','created_at','correction_attempt','provider_state','policy',
                'validation','coverage_counts','mismatching_units_count','issue_offset','unmapped_claims_count',
                'diagnostic_error')
        view = {key:report[key] for key in keys if key in report}
        if report.get('mismatching_units'):
            issue = report['mismatching_units'][0]
            first = {key:issue.get(key) for key in ('source_id','target_claim_id')}
            first.update(source_excerpt=safe(issue.get('source_excerpt',''),240),
                         retained_excerpt=safe(issue.get('retained_excerpt',''),240))
            reasons = dict(issue.get('reasons',{}))
            if reasons.get('canonical_token_difference'):
                diff = reasons['canonical_token_difference'][0]
                reasons['canonical_token_difference'] = {key:([safe(t,48) for t in value[:8]] if isinstance(value,list) else value)
                                                        for key,value in diff.items()}
            first['reasons'] = reasons
            view['first_mismatch'] = first
        elif report.get('schema_errors'):
            view['first_schema_error'] = report['schema_errors'][0]
        elif report.get('coverage_errors'):
            view['coverage_errors'] = {key:values[:1] for key,values in report['coverage_errors'].items()}
        elif report.get('unmapped_claims'):
            view['first_unmapped'] = report['unmapped_claims'][0]
        report = view
    report = bounded(report)
    encoded = json.dumps(report,ensure_ascii=False,default=str)
    cap = 3600 if compact else 24000
    while len(encoded) > cap and report.get('mismatching_units'):
        report['mismatching_units'].pop()
        report['output_truncated'] = True
        encoded = json.dumps(report,ensure_ascii=False,default=str)
    if len(encoded) > cap:
        # A huge ID/path or unmapped list must never bypass the output budget.
        keys = ('read_only','collection','position','run_id','state','error_code','candidates',
                'input_event_id','created_at','correction_attempt','provider_state',
                'validation','coverage_counts','mismatching_units_count','issue_offset','unmapped_claims_count')
        report = {key:report[key] for key in keys if key in report}
        report['output_truncated'] = True
        encoded = json.dumps(report,ensure_ascii=False,default=str)
        if len(encoded) > cap:
            encoded = json.dumps({'output_truncated':True,'input_event_id':report.get('input_event_id'),
                                  'mismatching_units_count':report.get('mismatching_units_count'),
                                  'unmapped_claims_count':report.get('unmapped_claims_count')})
    return encoded


def token_diff(before, after):
    changes = []
    for action, a, b, c, d in SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
        if action != 'equal':
            changes.append(dict(action=action, source_index=a, retained_index=c,
                source_tokens=[safe(t,80) for t in before[a:min(b,a+14)]],
                retained_tokens=[safe(t,80) for t in after[c:min(d,c+14)]],
                source_context=[safe(t,80) for t in before[max(0,a-4):min(len(before),a+18)]],
                retained_context=[safe(t,80) for t in after[max(0,c-4):min(len(after),c+18)]]))
    return changes[:5]


def diagnose_payload(packet, candidate, max_issues=12, issue_offset=0):
    if not 0 <= issue_offset <= 1000:
        raise ValueError('issue_offset must be between 0 and 1000')
    result = {'policy': packet.get('projection_protocol', {}).get('version')}
    originals = [HandoffClaim.model_validate(value) for value in packet['claims']]
    sources = packet['source_units']
    try:
        projection = FactorNeutralProjection.model_validate(candidate)
    except ValidationError as error:
        result['validation'] = 'schema_rejected'
        result['schema_errors'] = [dict(path=list(e['loc']),type=e['type'],message=safe(e['msg'],250))
                                   for e in error.errors(include_input=False)[:max_issues]]
        return result
    try:
        validate_factor_projection(projection, originals, sources)
        result['validation'] = 'accepted_by_current_validator'
    except ValueError as error:
        result['validation'] = safe(error,300)
    rewritten = {claim.claim_id:claim.neutral_text for claim in projection.rewrites}
    source_map = {source['source_id']:source for source in sources}
    covered = {identifier:set() for identifier in rewritten}
    failures = []
    missing = sorted(set(source_map) - {c.source_id for c in projection.coverage})
    unknown = sorted({c.source_id for c in projection.coverage} - set(source_map))
    duplicate = [key for key,count in Counter(c.source_id for c in projection.coverage).items() if count > 1]
    result['coverage_counts'] = dict(sources=len(sources), entries=len(projection.coverage),
        missing=len(missing), unknown=len(unknown), duplicate=len(duplicate))
    if missing or unknown or duplicate:
        result['coverage_errors'] = dict(missing=missing[:max_issues],unknown=unknown[:max_issues],duplicate=duplicate[:max_issues])
    for item in projection.coverage:
        source = source_map.get(item.source_id)
        if source is None:
            continue
        issue = dict(source_id=item.source_id, target_claim_id=item.target_claim_id)
        reasons = {}
        if item.disposition == 'omitted_author_evaluation':
            if not source['omittable'] or item.target_claim_id is not None or item.retained_excerpt is not None:
                reasons['invalid_omission'] = True
        else:
            text = rewritten.get(item.target_claim_id)
            excerpt = item.retained_excerpt
            if (text is None or (source['claim_id'] is not None and item.target_claim_id != source['claim_id'])
                    or not excerpt or excerpt not in text):
                reasons['invalid_corresponding_literal_excerpt'] = True
            elif source['omittable']:
                reasons['retained_author_verdict'] = True
            else:
                before = source['source_excerpt']
                source_content = re.sub(r'^\s*because\s+', '', before, flags=re.I) if source['author_evaluation_connector'] else before
                required = set(source['protected_terms'])
                actual = _protected_premises(excerpt)
                if required - actual:
                    reasons['missing_protected_terms'] = sorted(required - actual)
                if required & _MODAL_TERMS != actual & _MODAL_TERMS:
                    reasons['modal_difference'] = dict(source=sorted(required & _MODAL_TERMS),retained=sorted(actual & _MODAL_TERMS))
                if _quantities(before) != _quantities(excerpt):
                    reasons['quantities'] = dict(source=dict(_quantities(before)),retained=dict(_quantities(excerpt)))
                if _negative_anchors(before) != _negative_anchors(excerpt):
                    reasons['negative_anchors'] = dict(source=dict(_negative_anchors(before)),retained=dict(_negative_anchors(excerpt)))
                if _content_tokens(source_content) != _content_tokens(excerpt):
                    reasons['canonical_token_difference'] = token_diff(_content_tokens(source_content),_content_tokens(excerpt))
                positions, offset = [], 0
                while (start := text.find(excerpt, offset)) >= 0:
                    positions.append(start)
                    offset = start + 1
                start = max(positions, key=lambda position:sum(index not in covered[item.target_claim_id]
                    for index in range(position,position+len(excerpt))))
                covered[item.target_claim_id].update(range(start,start+len(excerpt)))
        if reasons:
            issue.update(source_excerpt=safe(source['source_excerpt']),
                         retained_excerpt=safe(item.retained_excerpt) if item.retained_excerpt is not None else None,
                         reasons=reasons)
            failures.append(issue)
    result['mismatching_units_count'] = len(failures)
    result['issue_offset'] = issue_offset
    result['mismatching_units'] = failures[issue_offset:issue_offset+max_issues]
    unmapped = []
    for identifier,text in rewritten.items():
        tokens = [(token,start,end) for token,start,end in _operational_tokens(text)
                  if not set(range(start,end)) <= covered[identifier]]
        if tokens:
            unmapped.append(dict(claim_id=identifier, count=len(tokens),
                first_tokens=[safe(token,80) for token,_,__ in tokens[:20]],
                first_context=safe(text[max(0,tokens[0][1]-80):tokens[0][2]+240],400)))
    result['unmapped_claims_count'] = len(unmapped)
    result['unmapped_claims'] = unmapped[:max_issues]
    return result


def parse_response(response):
    if not isinstance(response,dict):
        raise ValueError('Saved provider response is not an object')
    chunks = [content.get('text','') for item in response.get('output',[])
              for content in item.get('content',[]) if content.get('type') == 'output_text']
    text = ''.join(chunks)
    if not text:
        raise ValueError('Saved provider response has no output_text')
    if len(text) > 131072:
        raise ValueError('Saved provider output exceeds application limit')
    return json.loads(text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--collection',required=True)
    parser.add_argument('--position',required=True,type=int,choices=range(1,21))
    parser.add_argument('--max-candidates',type=int,default=3,choices=range(1,11))
    parser.add_argument('--max-issues',type=int,default=8,choices=range(1,21))
    parser.add_argument('--issue-offset',type=int,default=0,choices=range(0,1001),metavar='0..1000',
                        help='Skip this many mismatching source units; the total count remains unchanged')
    parser.add_argument('--compact',action='store_true',help='Console-friendly first mismatch per candidate; omit for fuller JSON diagnostics')
    args = parser.parse_args()
    dsn = os.environ.get('DATABASE_URL')
    if not dsn:
        raise ValueError('DATABASE_URL is not set')
    # Force read-only at connection creation and transaction start. Do not use
    # application ledger/execution helpers: those can take leases or write events.
    with psycopg.connect(dsn,autocommit=True,row_factory=dict_row,
                         options='-c default_transaction_read_only=on -c statement_timeout=15000') as conn:
        conn.execute('BEGIN READ ONLY')
        try:
            if conn.execute('SHOW transaction_read_only').fetchone()['transaction_read_only'] != 'on':
                raise ValueError('Read-only transaction was not established')
            item = conn.execute('''SELECT run_id,state,error_code FROM app.collection_items
                WHERE collection_id=%s AND position=%s''',(args.collection,args.position)).fetchone()
            if not item or not item['run_id']:
                raise ValueError('Collection position has no research run')
            rows = conn.execute('''SELECT * FROM (
                SELECT DISTINCT ON (payload->>'operation_key') id,created_at,payload
                FROM app.events WHERE run_id=%s AND kind='role_input'
                AND payload->>'role'='repair' AND payload->'input' ? 'projection_protocol'
                ORDER BY payload->>'operation_key',id DESC
                ) inputs ORDER BY id DESC LIMIT %s''',(item['run_id'],args.max_candidates)).fetchall()
            print(render_report(dict(read_only=True,collection=args.collection,position=args.position,
                run_id=item['run_id'],state=item['state'],error_code=item['error_code'],candidates=len(rows)),args.compact))
            for row in reversed(rows):
                payload = row['payload']; operation_key = payload['operation_key']
                operation = conn.execute('''SELECT state,response FROM app.provider_operations
                    WHERE run_id=%s AND operation_key=%s''',(item['run_id'],operation_key)).fetchone()
                rejection = conn.execute('''SELECT payload->'errors' AS errors FROM app.events
                    WHERE run_id=%s AND kind='role_output_rejected' AND payload->>'operation_key'=%s
                    ORDER BY id DESC LIMIT 1''',(item['run_id'],operation_key)).fetchone()
                report = dict(input_event_id=row['id'],created_at=str(row['created_at']),
                    correction_attempt=payload.get('correction_attempt'),
                    operation_key=operation_key,provider_state=operation['state'] if operation else 'absent')
                if rejection:
                    report['recorded_errors'] = [dict(code=e.get('code'),path=e.get('path'),message=safe(e.get('message'),300))
                                                 for e in rejection['errors'][:3]]
                try:
                    report.update(diagnose_payload(payload['input'],parse_response(operation['response']),
                                                   args.max_issues,args.issue_offset))
                except Exception as error:
                    report['diagnostic_error'] = dict(type=type(error).__name__,message=safe(error,300))
                # All emitted content is bounded claim/excerpt diagnostics; never
                # print database URLs, entire packets, documents or provider data.
                print(render_report(report,args.compact))
        finally:
            conn.execute('ROLLBACK')


if __name__ == '__main__':
    try:
        main()
    except (psycopg.Error,ValueError,KeyError,TypeError) as error:
        # Connection errors can contain a password-bearing DSN; omit their text.
        print(json.dumps(dict(fatal=type(error).__name__,message='Read-only diagnostic unavailable; inspect the supplied collection/position and local environment.')))
        sys.exit(1)
