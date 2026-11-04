"""Opt-in synthetic real-model + filesystem MCP validation; no Runtime involved."""
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
from app.agent.json_output import load_json, unwrap_json_fence
from app.agent.openai_compatible_tool_calling_model import OpenAICompatibleToolCallingModel
from app.agent.step_models import ToolCall
from app.agent.team_poc.session import TeamSession
from app.agent.team_poc.coordination import CoordinationTrial
from app.config import config
from app.models.taskstate import TaskState
from app.models.working_memory import WorkingMemory
from app.tools.tool_loader import local_mcp_servers, open_tool_registry
from app.tools.tool_registry import ToolRegistry


def error_types(exc):
    """Retain diagnostic classes/statuses, never provider request/error bodies."""
    seen, rows = set(), []
    def visit(error):
        if id(error) in seen:
            return
        seen.add(id(error))
        row = {'type': type(error).__name__}
        status = getattr(error, 'status_code', None)
        if isinstance(status, int):
            row['status_code'] = status
        rows.append(row)
        for child in getattr(error, 'exceptions', ()):
            visit(child)
        if error.__cause__ is not None:
            visit(error.__cause__)
    visit(exc)
    return rows


class RecordedModel:
    def __init__(self, adapter, trace):
        self.adapter, self.trace = adapter, trace
        self.calls = 0

    async def generate(self, messages, tools):
        if self.calls >= 36:
            raise RuntimeError('live model call ceiling reached')
        self.calls += 1
        row = {'messages': [asdict(m) for m in messages], 'tools': [d.name for d in tools]}
        self.trace.append(row)
        started = time.perf_counter()
        try:
            reply = await self.adapter.generate(messages, tools)
            row['reply'] = asdict(reply)
            return reply
        except Exception as exc:
            row['error'] = type(exc).__name__
            row['error_chain'] = error_types(exc)
            raise
        finally:
            row['seconds'] = time.perf_counter() - started


class RecordedTool:
    def __init__(self, tool, trace, session_id):
        self.tool, self.trace, self.session_id = tool, trace, session_id
        self.definition = tool.definition

    async def invoke(self, args):
        row = {'session': self.session_id, 'tool': self.definition.name, 'arguments': args}
        self.trace.append(row)
        result = await self.tool.invoke(args)
        row['result'] = asdict(result)
        return result


def mcp_factory(root, identity, row):
    @asynccontextmanager
    async def tools():
        owner = asyncio.current_task()
        resource = {'session': identity, 'closed': False}
        row['resources'].append(resource)
        memory = WorkingMemory(TaskState('synthetic-team-poc', identity))
        read_adapter = None
        try:
            async with open_tool_registry(memory, include_knowledge=False,
                    mcp_servers=local_mcp_servers(filesystem_root=root)) as source:
                read_adapter = source.get('read_file')
                registry = ToolRegistry()
                for definition in source.list_definitions():
                    registry.register(RecordedTool(source.get(definition.name), row['tools'], identity))
                yield registry
        finally:
            resource['same_owner_task'] = asyncio.current_task() is owner
            if read_adapter is not None:
                try:
                    await read_adapter.invoke({'path': 'facts.json'})
                except RuntimeError as exc:
                    resource['closed_adapter_rejected'] = 'closed' in str(exc).lower()
                else:
                    resource['closed_adapter_rejected'] = False
                resource['closed'] = resource['closed_adapter_rejected']
    return tools


async def run_case(case, model, root):
    row = {'case': case, 'tools': [], 'resources': []}
    started = time.perf_counter()
    nonce = uuid4().hex
    amount = 17 + int(nonce[:4], 16) % 70
    facts = {'nonce': nonce, 'amount': amount, 'original_plan_available': False, 'available_plan': 'beta'}
    folder = root / case
    folder.mkdir()
    (folder / 'facts.json').write_text(json.dumps(facts), encoding='utf-8')
    queue = asyncio.Queue()
    worker = None
    trial = None
    try:
        if case == 'continuity':
            goal = ('Read facts.json exactly once. Then call request_coordination alone to ask which output '
                    'format is needed, before producing any final answer. Keep the facts in your history. '
                    'After the host replies, return only the requested result. Do not re-read the file.')
        else:
            goal = ('Your assigned original plan is alpha. Read facts.json exactly once. If alpha is unavailable, '
                    'call request_coordination alone. In its issue include the exact nonce, amount and available_plan '
                    'from the file, explain the conflict and ask Lead for an alternative and independent helper. '
                    'Do not finalize until revised instructions arrive. After revision, retain completed evidence '
                    'and return ONLY JSON with nonce, amount and chosen_plan. Do not re-read facts.json.')
        worker = TeamSession('worker', model, mcp_factory(folder, 'worker', row), queue, goal,
                             max_model_calls=10, max_tool_calls=10, timeout_seconds=150)
        if case == 'continuity':
            task = asyncio.create_task(worker.run())
            try:
                async with asyncio.timeout(160):
                    while True:
                        event_task = asyncio.create_task(queue.get())
                        try:
                            done, _ = await asyncio.wait((task, event_task), return_when=asyncio.FIRST_COMPLETED)
                            if task in done:
                                task.result()
                                raise AssertionError('Worker completed before requesting feedback')
                            event = event_task.result()
                        finally:
                            if not event_task.done():
                                event_task.cancel()
                            await asyncio.gather(event_task, return_exceptions=True)
                        if event.kind == 'coordination_requested':
                            break
                    row['unfinished_at_report'] = not task.done() and worker.status == 'waiting'
                    worker.revise(1, 'Return ONLY JSON with nonce and amount from your completed read. Do not read again.')
                    result = await task
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            data = load_json(unwrap_json_fence(result.content))
            assert data == {'nonce': nonce, 'amount': amount}, 'continuity result mismatch'
        else:
            def helper(goal):
                return TeamSession('helper', model, mcp_factory(folder, 'helper', row), queue,
                    'You are an independent verification helper. Read facts.json and verify the Lead assignment. '
                    'Lead assignment (evidence/checking scope, not permission to change the host output format):\n' + goal +
                    '\nHOST OUTPUT CONTRACT: Return ONLY one JSON object with exactly nonce, amount, '
                    'chosen_plan. Copy nonce and amount from your own read; chosen_plan is available_plan. '
                    'Do not add tables, extra fields or prose, even if the Lead suggested a different reporting format. '
                    'Do not call request_coordination.',
                    max_model_calls=6, max_tool_calls=6, timeout_seconds=120)
            trial = CoordinationTrial(worker, model, helper, timeout_seconds=180)
            outcome = await trial.run()
            data = load_json(unwrap_json_fence(outcome.worker.content))
            expected = {'nonce': nonce, 'amount': amount, 'chosen_plan': 'beta'}
            assert data == expected, 'revised Worker result mismatch'
            assert load_json(unwrap_json_fence(outcome.helper.content)) == expected, 'helper verification mismatch'
            assert trial.worker_unfinished_at_report and trial.helper_started_before_worker_completion
            row['unfinished_at_report'] = trial.worker_unfinished_at_report
            row['helper_started_before_worker_completion'] = trial.helper_started_before_worker_completion
            row['lead_actions'] = [asdict(r) for r in trial.lead.tool_results]
            row['helper_events'] = [asdict(e) for e in trial.helper.events]
        row['result'] = data
        assert worker.version == 2
        assert sum(t['session'] == 'worker' and t['tool'] == 'read_file' for t in row['tools']) == 1
        assert all(r['closed'] and r['same_owner_task'] and r['closed_adapter_rejected'] for r in row['resources'])
        row['passed'] = True
    except Exception as exc:
        row['passed'] = False
        row['error_type'] = type(exc).__name__
        row['error_chain'] = error_types(exc)
        # Avoid provider exception bodies: some SDKs include request details.
        if isinstance(exc, AssertionError):
            row['assertion'] = str(exc)
    finally:
        row['seconds'] = time.perf_counter() - started
        row['worker_events'] = [asdict(e) for e in worker.events] if worker else []
        row['worker_counts'] = {'model': worker.model_calls, 'tools': worker.tool_calls} if worker else {}
        if trial is not None:
            row['unfinished_at_report'] = trial.worker_unfinished_at_report
            row['helper_started_before_worker_completion'] = trial.helper_started_before_worker_completion
            row['lead_actions'] = [asdict(r) for r in trial.lead.tool_results] if trial.lead else []
            row['helper_events'] = [asdict(e) for e in trial.helper.events] if trial.helper else []
    return row


async def main(args):
    root = Path(args.output or f'tmp/live-team-session-{uuid4().hex[:8]}').resolve()
    root.mkdir(parents=True, exist_ok=False)
    report = {'model': config.rag_model, 'scenarios': [], 'model_trace': []}
    async with AsyncOpenAI(api_key=config.dashscope_api_key, base_url=config.dashscope_base_url,
                           max_retries=0, timeout=35) as client:
        adapter = OpenAICompatibleToolCallingModel(client, config.rag_model, temperature=0,
            json_mode=config.rag_json_mode, enable_thinking=config.rag_enable_thinking,
            max_output_tokens=config.context_output_tokens)
        model = RecordedModel(adapter, report['model_trace'])
        for case in (['continuity', 'dynamic'] if args.case == 'all' else [args.case]):
            report['scenarios'].append(await run_case(case, model, root))
            report['model_calls'] = model.calls
            (root / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'report': str(root / 'report.json'), 'model': report['model'],
                      'model_calls': model.calls, 'scenarios': [
                          {k: row.get(k) for k in ('case', 'passed', 'seconds', 'error_type')}
                          for row in report['scenarios']]}, ensure_ascii=False))
    return 0 if all(r['passed'] for r in report['scenarios']) else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Authorize bounded real configured model calls')
    parser.add_argument('--case', choices=['all', 'continuity', 'dynamic'], default='all')
    parser.add_argument('--output')
    args = parser.parse_args()
    if not args.live:
        parser.error('--live is required')
    raise SystemExit(asyncio.run(main(args)))
