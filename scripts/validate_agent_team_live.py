"""Bounded real Planner -> Runtime -> AgentTeamRunner -> MCP -> reviewed result."""
import argparse
import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
import json
from pathlib import Path
import sys
import time
from uuid import uuid4
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from openai import AsyncOpenAI
from app.config import config
from app.agent.openai_compatible_tool_calling_model import OpenAICompatibleToolCallingModel
from app.agent.team.contracts import JsonContract, ContractError
from app.agent.team.models import TeamOptions, TeamStore
from app.models.taskstate import TaskStatus
from app.services.rag_agent_service import RagAgentService
import app.services.rag_agent_service as service_module
from app.tools.tool_loader import local_mcp_servers
from app.tools.tool_registry import ToolRegistry
from validate_team_session_poc import error_types


class ModelTrace:
    def __init__(self, adapter):
        self.adapter, self.trace, self.calls = adapter, [], 0

    async def generate(self, messages, tools):
        if self.calls >= 96:
            raise RuntimeError('live model request ceiling reached')
        self.calls += 1
        row = {'messages': [asdict(m) for m in messages], 'tools': [t.name for t in tools]}
        self.trace.append(row)
        try:
            result = await self.adapter.generate(messages, tools)
            row['reply'] = asdict(result)
            return result
        except Exception as exc:
            row['error_chain'] = error_types(exc)
            raise


class ObservedStore(TeamStore):
    def __init__(self):
        super().__init__()
        self.observed = []

    def get(self, context, options):
        state = super().get(context, options)
        if all(s is not state for s in self.observed):
            self.observed.append(state)
        return state


class ObservedTool:
    def __init__(self, tool, memory, rows):
        self.tool, self.memory, self.rows = tool, memory, rows
        self.definition = tool.definition

    async def invoke(self, arguments):
        row = {'memory_task_id': self.memory.task_state.task_id, 'name': self.definition.name,
               'arguments': arguments}
        self.rows.append(row)
        result = await self.tool.invoke(arguments)
        row['result'] = asdict(result)
        return result


async def scenario(case, model, root):
    started = time.perf_counter()
    row = {'case': case, 'tools': [], 'connections': [], 'runtime_results': []}
    folder = root / case
    folder.mkdir()
    nonce = uuid4().hex
    expected = {'nonce': nonce, 'amount': 20 + int(nonce[:4], 16) % 50, 'chosen_plan': 'beta'}
    facts = dict(expected, available_plan='beta', original_plan_available=False)
    del facts['chosen_plan']
    (folder / 'facts.json').write_text(json.dumps(facts), encoding='utf-8')
    def check(payload):
        if payload != expected:
            raise ContractError('final result does not match source facts')
    contract = JsonContract('host-final', (('nonce', str), ('amount', int), ('chosen_plan', str)), check)
    store, runtimes = ObservedStore(), []
    original_tools, original_runtime = service_module.open_tool_registry, service_module.AgentRuntime
    @asynccontextmanager
    async def traced_tools(memory, **kwargs):
        owner = asyncio.current_task()
        life = {'memory_task_id': memory.task_state.task_id, 'closed': False}
        row['connections'].append(life)
        adapter = None
        try:
            async with original_tools(memory, **kwargs) as registry:
                adapter = registry.get('read_file') if any(d.name == 'read_file' for d in registry.list_definitions()) else None
                observed = ToolRegistry()
                for d in registry.list_definitions():
                    observed.register(ObservedTool(registry.get(d.name), memory, row['tools']))
                yield observed
        finally:
            life['same_owner_task'] = asyncio.current_task() is owner
            if adapter is not None:
                try:
                    await adapter.invoke({'path': 'facts.json'})
                except RuntimeError as exc:
                    life['closed'] = 'closed' in str(exc).lower()
    def runtime_factory(*args, **kwargs):
        runtime = original_runtime(*args, **kwargs)
        runtimes.append(runtime)
        return runtime
    options = TeamOptions(output_contract=contract, timeout_seconds=300, max_actions=32,
                          allowed_tools=frozenset({'read_file', 'list_directory', 'get_current_time'}))
    service = RagAgentService(model=model, execution_mode='team', team_options=options,
        mcp_servers=local_mcp_servers(filesystem_root=folder), include_knowledge=False,
        memory_directory=str(folder / 'history'), max_replans=2)
    service._team_store = store
    goal = (
        'Produce ONE team-managed final deliverable. Planner: use ONE concise top-level step '
        'description under 120 characters, such as "Produce a reviewed report with a team". '
        'Keep detailed requirements in this goal rather than copying them into the step text. '
        'Plan this as ONE top-level step, with all '
        'retrieval, feedback and review performed inside that team step, not separate top-level steps. '
        'Use the filesystem MCP facts.json. Team Lead must dynamically create a worker with read_file '
        'and a separate reviewer with read_file. Worker reads facts.json exactly once, reports via '
        'request_coordination that assigned original plan alpha is unavailable and includes the facts. '
        'Lead must adjust that SAME worker assignment, preserving completed evidence. '
        'Final candidate uses nonce/amount from source and chosen_plan equal to available_plan. '
        'Reviewer must independently read facts.json and check each candidate field. If incorrect, '
        'Lead orders targeted rework, then review the new version. Use the host JSON schema exactly. '
        'Do not reread completed source in the worker. ')
    if case == 'pause':
        goal += ('IMPORTANT: beta requires explicit USER authorization. After the Worker has read the '
                 'file and reported, Lead must use ask_user to request permission to select beta. Do '
                 'not finish or authorize beta yourself before that user reply. After user authorizes, '
                 'resume the existing team and worker and finish the same deliverable.')
    else:
        goal += 'Lead may authorize the available beta plan without asking the user.'
    if case == 'rework':
        goal += (' This is an explicitly seeded negative-control review test on synthetic data, '
                 'not a real transaction. Require the first Worker candidate to use source amount+1 '
                 'while keeping nonce and beta correct. Submit that seed to independent review '
                 'UNCHANGED. Reviewer must reject the amount mismatch; Lead then assigns correction '
                 'to the SAME Worker, runs it using retained evidence, and reviews the NEW version '
                 'before finish. Do not correct the seed before its first review.')
    try:
        with patch.object(service_module, 'open_tool_registry', traced_tools), patch.object(
                service_module, 'AgentRuntime', runtime_factory):
            result = await service.query_result(goal, case, user_id='synthetic-team', request_id='initial')
            row['runtime_results'].append(asdict(result))
            if case == 'pause':
                assert result.status is TaskStatus.BLOCKED, 'expected user pause'
                assert len(store.observed) == 1, 'team state missing before pause'
                state = store.observed[0]
                before_ids = {n: m.memory.task_state.task_id for n, m in state.members.items()}
                assert any(t['name'] == 'read_file' for t in row['tools']), 'paused before Worker evidence read'
                assert all(c['closed'] and c['same_owner_task'] for c in row['connections'])
                task_id = result.task_id
                old_binding = runtimes[0]._runner
                result = await service.query_result('I authorize beta. Continue using the saved evidence.', case,
                    user_id='synthetic-team', request_id='resume', resume_task_id=task_id)
                row['runtime_results'].append(asdict(result))
                assert result.task_id == task_id and runtimes[0]._runner is not old_binding
                assert all(state.members[n].memory.task_state.task_id == mid for n, mid in before_ids.items())
                row['same_team_resumed'] = True
            assert result.status is TaskStatus.COMPLETED, 'Agent did not complete'
            assert len(store.observed) == 1 and len(result.steps) == 1, 'expected one team-managed deliverable'
            assert json.loads(result.content) == expected
            state = store.observed[0]
            assert state.status == 'completed'
            assert any(m.role == 'reviewer' and m.review for m in state.members.values())
            if case == 'rework':
                review_history = [h for h in state.history if h['action'] == 'review']
                assert len(review_history) >= 2, 'seed was not reviewed and then reviewed again'
                worker_events = [e for m in state.members.values() if m.role == 'worker' for e in m.session.events]
                drafts = [json.loads(e.detail) for e in worker_events if e.kind == 'candidate_submitted']
                assert len(drafts) >= 2 and drafts[0]['amount'] == expected['amount'] + 1
                assert any(h['action'] == 'assign' for h in state.history), 'no directed rework assignment'
            for name, member in state.members.items():
                reads = [t for t in row['tools'] if t['memory_task_id'] == member.memory.task_state.task_id
                         and t['name'] == 'read_file' and t['arguments'].get('path') == 'facts.json']
                if member.role == 'worker':
                    assert len(reads) == 1, 'Worker repeated or skipped original read'
            assert any(t['name'] == 'read_file' and any(m.role == 'reviewer' and
                       m.memory.task_state.task_id == t['memory_task_id'] for m in state.members.values())
                       for t in row['tools']), 'Reviewer did not independently read'
            assert all(c['closed'] and c['same_owner_task'] for c in row['connections'])
            assert all(d.runner_id == 'agent_team' for rt in runtimes for d in rt.routing_decisions)
            row['passed'] = True
    except Exception as exc:
        row['passed'] = False
        row['error_chain'] = error_types(exc)
        if isinstance(exc, AssertionError):
            row['assertion'] = str(exc)
    finally:
        await service.aclose()
        row['seconds'] = time.perf_counter() - started
        row['teams'] = [{'status': s.status, 'key': s.key, 'actions': s.history, 'budget': asdict(s.budget),
            'members': {name: {'role': m.role, 'version': m.session.version, 'events': [asdict(e) for e in m.session.events],
                'review': m.review} for name, m in s.members.items()}} for s in store.observed]
        row['routes'] = [[asdict(d) for d in rt.routing_decisions] for rt in runtimes]
    return row


async def main(args):
    root = Path(args.output or f'tmp/live-agent-team-{uuid4().hex[:8]}').resolve()
    root.mkdir(parents=True, exist_ok=False)
    report = {'model': config.rag_model, 'cases': []}
    async with AsyncOpenAI(api_key=config.dashscope_api_key, base_url=config.dashscope_base_url,
                           timeout=35, max_retries=0) as client:
        adapter = OpenAICompatibleToolCallingModel(client, config.rag_model, temperature=0,
            json_mode=config.rag_json_mode, enable_thinking=config.rag_enable_thinking,
            max_output_tokens=config.context_output_tokens)
        model = ModelTrace(adapter)
        for case in (['complete', 'pause', 'rework'] if args.case == 'all' else [args.case]):
            report['cases'].append(await scenario(case, model, root))
            report['model_calls'], report['model_trace'] = model.calls, model.trace
            (root / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print(json.dumps({'report': str(root / 'report.json'), 'calls': model.calls,
        'cases': [{k: r.get(k) for k in ('case', 'passed', 'seconds', 'assertion', 'error_chain')} for r in report['cases']]}))
    return 0 if all(r['passed'] for r in report['cases']) else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--case', choices=['all', 'complete', 'pause', 'rework'], default='all')
    parser.add_argument('--output')
    args = parser.parse_args()
    if not args.live:
        parser.error('--live is required')
    raise SystemExit(asyncio.run(main(args)))
