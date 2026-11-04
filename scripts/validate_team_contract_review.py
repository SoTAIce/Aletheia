"""Opt-in strict synthetic multi-feedback/review/rework validation on real MCP."""
import argparse
import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from openai import AsyncOpenAI
from app.config import config
from app.agent.openai_compatible_tool_calling_model import OpenAICompatibleToolCallingModel
from app.agent.team_poc.contracts import JsonContract, ContractError
from app.agent.team_poc.session import TeamSession
from app.agent.team_poc.reviews import ReviewTrial, DECISION_PROTOCOL, REVIEW_PROTOCOL
from app.tools.tool_registry import ToolRegistry
from validate_team_session_poc import RecordedModel, mcp_factory, error_types


@asynccontextmanager
async def no_business_tools():
    yield ToolRegistry()


async def run_case(case, model, root):
    start = time.perf_counter()
    row = {'case': case, 'tools': [], 'resources': [], 'negative_control': case == 'rework'}
    folder = root / case
    folder.mkdir()
    nonce = uuid4().hex
    amount = 20 + int(nonce[:4], 16) % 60
    facts = {'nonce': nonce, 'amount': amount, 'available_plan': 'beta', 'original_plan_available': False}
    (folder / 'facts.json').write_text(json.dumps(facts), encoding='utf-8')
    expected = {'nonce': nonce, 'amount': amount, 'chosen_plan': 'beta'}
    fields = (('nonce', str), ('amount', int), ('chosen_plan', str))
    member_protocol = JsonContract('candidate', fields)
    def verify(payload):
        if payload != expected:
            raise ContractError('candidate does not match immutable synthetic source facts')
    host_contract = JsonContract('host-final', fields, verify)
    events = asyncio.Queue()
    worker = lead = trial = None
    reviewers = []
    try:
        goal = (
            'Read facts.json once and retain its contents. Your original plan is alpha, which is unavailable. '
            'Call request_coordination alone, report all facts and ask Lead to authorize the available plan. '
            'After feedback, use submit_result to submit your candidate: nonce and amount from facts, '
            'chosen_plan set to the authorized available plan. A submitted draft waits for host review. '
            'On rejection correct it in this same Session using existing evidence, do not re-read the file. ')
        if case == 'feedback':
            goal += ('For this controlled multi-feedback experiment, after the first Lead revision make one '
                     'additional request_coordination asking whether to retain the same original evidence '
                     'and output fields. Submit only after that second response. This is a directed protocol test.')
        else:
            goal += ('This is an EXPLICITLY SEEDED NEGATIVE-CONTROL review experiment, not a real transaction. '
                     'After the first Lead response, the FIRST candidate must reproduce the supplied incorrect '
                     'draft unchanged so the independent reviewer can reject it. Do not fix the seed before '
                     'the first review. After rejection repair it from your retained source facts. Seed draft: ' +
                     json.dumps(dict(expected, amount=amount + 1)))
        worker = TeamSession('worker', model, mcp_factory(folder, 'worker', row), events, goal,
                             output_contract=member_protocol, reviewable=True,
                             max_model_calls=12, max_tool_calls=16, timeout_seconds=180)
        lead = TeamSession('lead', model, no_business_tools, events,
            'You are the independent Team Lead. Host events contain Worker issues or a candidate plus '
            'independent review and host acceptance check. Use submit_result for your decision. '
            'For coordination requests choose revise with concrete instructions resolving the issue. '
            'Authorize beta when source says available_plan=beta. Preserve host assignment and any seeded '
            'negative-control experiment: do not prematurely correct the first seed. For review rejection or '
            'host_rejection choose revise and explain the correction. Only accept when review approves '
            'AND host_rejection is null. Internal review may use detailed narrative feedback in its protocol '
            'feedback field; review_focus can specify detailed prose analysis, not override protocol fields. '
            'Do not request_coordination yourself. Do not require unavailable external messages.',
            output_contract=DECISION_PROTOCOL, reviewable=True, max_model_calls=10,
            max_tool_calls=12, timeout_seconds=180)
        def reviewer(candidate, focus):
            identity = 'review-' + str(candidate.version)
            session = TeamSession(identity, model, mcp_factory(folder, identity, row), events,
                'Independently read facts.json exactly once, compare all candidate fields with the source. '
                'Use submit_result with candidate_version, decision and feedback. decision is approve only '
                'if nonce matches, amount matches exactly and chosen_plan equals available_plan; otherwise '
                'decision is revise. feedback may contain detailed prose requested by Lead. Do not change '
                'the assigned candidate_version. Do not call request_coordination. Candidate:\n' +
                json.dumps({'candidate_version': candidate.version, 'payload': json.loads(candidate.content)}) +
                '\nLead internal review focus:\n' + focus,
                output_contract=REVIEW_PROTOCOL, max_model_calls=5, max_tool_calls=6, timeout_seconds=90)
            reviewers.append(session)
            return session
        trial = ReviewTrial(worker, lead, reviewer, host_contract, max_cycles=5, timeout_seconds=210)
        result = await trial.run()
        actual = json.loads(result.content)
        assert actual == expected, 'strict final host JSON does not match source'
        assert sum(t['session'] == 'worker' and t['tool'] == 'read_file' for t in row['tools']) == 1, 'Worker re-read'
        assert all(sum(t['session'] == r.session_id and t['tool'] == 'read_file' for t in row['tools']) == 1
                   for r in reviewers), 'independent reviewer did not read exactly once'
        assert all(r['closed'] and r['same_owner_task'] and r['closed_adapter_rejected'] for r in row['resources'])
        assert trial.reviews[-1]['decision'] == 'approve'
        if case == 'feedback':
            assert sum(e.kind == 'coordination_requested' for e in worker.events) == 2
            assert lead.version >= 3 and worker.version >= 3
        else:
            drafts = [json.loads(e.detail) for e in worker.events if e.kind == 'candidate_submitted']
            assert len(drafts) >= 2 and drafts[0]['amount'] == amount + 1, 'negative-control seed was not submitted'
            assert trial.reviews[0]['decision'] == 'revise', 'reviewer did not reject seeded error'
            assert trial.reviews[-1]['candidate_version'] > trial.reviews[0]['candidate_version']
        row['final'] = actual
        row['passed'] = True
    except Exception as exc:
        row.update(passed=False, error_chain=error_types(exc))
        if isinstance(exc, AssertionError):
            row['assertion'] = str(exc)
    finally:
        row['seconds'] = time.perf_counter() - start
        row['worker_events'] = [asdict(e) for e in worker.events] if worker else []
        row['lead_events'] = [asdict(e) for e in lead.events] if lead else []
        row['reviewer_events'] = [[asdict(e) for e in r.events] for r in reviewers]
        row['reviews'] = trial.reviews if trial else []
        row['decisions'] = trial.decisions if trial else []
        row['cycles'] = trial.cycles if trial else 0
        row['repairs'] = {s.session_id: s.format_repairs for s in [worker, lead, *reviewers] if s}
    return row


async def main(args):
    root = Path(args.output or f'tmp/live-team-contract-{uuid4().hex[:8]}').resolve()
    root.mkdir(parents=True, exist_ok=False)
    report = {'model': config.rag_model, 'cases': [], 'model_trace': []}
    async with AsyncOpenAI(api_key=config.dashscope_api_key, base_url=config.dashscope_base_url,
                           max_retries=0, timeout=35) as client:
        adapter = OpenAICompatibleToolCallingModel(client, config.rag_model, temperature=0,
            json_mode=config.rag_json_mode, enable_thinking=config.rag_enable_thinking,
            max_output_tokens=config.context_output_tokens)
        model = RecordedModel(adapter, report['model_trace'])
        for case in (['feedback', 'rework'] if args.case == 'all' else [args.case]):
            report['cases'].append(await run_case(case, model, root))
            report['model_calls'] = model.calls
            (root / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'report': str(root / 'report.json'), 'model_calls': model.calls,
                      'cases': [{k: row.get(k) for k in ('case', 'passed', 'seconds', 'assertion', 'error_chain')}
                                for row in report['cases']]}))
    return 0 if all(row['passed'] for row in report['cases']) else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--case', choices=['all', 'feedback', 'rework'], default='all')
    parser.add_argument('--output')
    args = parser.parse_args()
    if not args.live:
        parser.error('--live is required')
    raise SystemExit(asyncio.run(main(args)))
