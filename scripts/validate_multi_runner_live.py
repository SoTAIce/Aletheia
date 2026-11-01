"""Opt-in real-model/real-filesystem-MCP lifecycle validation on isolated fixtures."""
import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from openai import AsyncOpenAI
from app.agent.openai_compatible_tool_calling_model import OpenAICompatibleToolCallingModel
from app.agent.planner import Planner
from app.agent.planning_model import PlanningModel
from app.agent.runtime import AgentRuntime
from app.agent.step_models import ToolCall
from app.agent.step_runner import StepToolError
from app.config import config
from app.models.taskstate import TaskState, TaskStatus
from app.models.working_memory import WorkingMemory
from app.services.context_manager import ContextManager
from app.tools.tool_loader import local_mcp_servers, open_tool_registry


def encode(value):
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    raise TypeError(type(value).__name__)


class RecordedModel:
    def __init__(self, model, events):
        self.model, self.events = model, events

    async def generate(self, messages, tools):
        if len(self.events) >= 80:
            raise RuntimeError('Live validation model-call ceiling reached')
        row = {'messages': [asdict(m) for m in messages], 'tools': [d.name for d in tools]}
        started = time.perf_counter()
        try:
            turn = await self.model.generate(messages, tools)
            row['reply'] = asdict(turn)
            return turn
        except Exception as exc:
            row['error'] = type(exc).__name__
            raise
        finally:
            row['seconds'] = time.perf_counter()-started
            self.events.append(row)


def traced_registry(registry, row, phase, *, fail_fast_missing=False):
    invoke = registry.invoke
    async def traced(call):
        event = dict(phase=phase, call=asdict(call))
        start = time.perf_counter()
        try:
            result = await invoke(call)
            event['result'] = asdict(result)
            # Real MCP missing-file errors can otherwise be recovered inside
            # one Runner round. Promote ONLY that actual error at the test
            # boundary to guarantee exercise of Runtime's Replan branch.
            if fail_fast_missing and call.tool_name == 'read_file' and call.arguments.get('path') == 'primary.txt' and not result.success:
                event['injected_fail_fast'] = True
                raise StepToolError('primary.txt is absent (real MCP failure); replan using backup.txt')
            return result
        except Exception as exc:
            event['error'] = type(exc).__name__
            raise
        finally:
            event['seconds'] = time.perf_counter()-start
            row['tool_events'].append(event)
    registry.invoke = traced


async def closed_probe(registry):
    try:
        await registry.get('read_file').invoke({'path': 'resume.txt'})
    except RuntimeError as exc:
        return 'closed' in str(exc).lower()
    return False


def count_reads(row, path):
    return sum(e['call']['tool_name'] == 'read_file' and e['call']['arguments'].get('path') == path
               for e in row['tool_events'])


def read_output(root, name):
    path = root / name
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}


async def run_case(root, name, model):
    files = root / name
    files.mkdir()
    nonce = uuid4().hex[:10]
    (files/'metadata.json').write_text(json.dumps({'reference': nonce, 'currency': 'CNY'}), encoding='utf-8')
    (files/'prices.csv').write_text('item,quantity,unit_price\npaper,3,12\nink,2,35\n', encoding='utf-8')
    (files/'resume.txt').write_text('Evidence token: '+nonce, encoding='utf-8')
    (files/'backup.txt').write_text(json.dumps({'recovery_key': nonce}), encoding='utf-8')
    goals = {
        'multi': '这是实际文件处理任务，不能模拟工具结果。请计划三个独立步骤：'
                 '第一步只读取metadata.json和prices.csv，计算quantity乘unit_price的总额，并调用get_current_time获取UTC时间，保存结果；'
                 '第二步根据第一步证据使用write_file创建report.json，包含reference、currency、total、timestamp；'
                 '第三步必须调用read_file读回report.json验证。只读取这两个输入文件，不需要其他文件。'
                 'reference只能来自metadata.json。所有路径相对根目录。文件JSON是写入内容，不替代每一步的执行状态报告。',
        'pause': '这是分阶段文件任务，必须独立完成第一步后才能询问。第一步只读取resume.txt，提取Evidence token并作为该步骤成功结果保存。'
                 '第二步需要用户选择输出格式，但当前格式尚未指定：必须报告blocked并提出格式问题，不得自行决定格式或写文件。'
                 '恢复时使用已保存的token，不得再次读取resume.txt；用户选择JSON后，写入resume-result.json，字段token为之前读取的值，'
                 '再读取输出文件验证。计划必须将读取证据和等待格式分成不同步骤。',
        'replan': '真实故障恢复任务。初始步骤先尝试read_file读取primary.txt，不能假设它存在。'
                  '如果该步骤因文件缺失失败，Runtime会重新规划：新的计划必须改读backup.txt，不要再尝试primary.txt。'
                  '从真实backup.txt提取recovery_key，然后write_file创建recovery.json保存该字段，并read_file读回验证。'
                  '只使用真实工具证据，不得虚构内容；每步结果须与实际执行一致。',
    }
    row = dict(case=name, nonce=nonce, tool_events=[], results=[], checks={})
    memory = WorkingMemory(TaskState('live-validation', goals[name]))
    servers = local_mcp_servers(filesystem_root=files, write_root=files)
    manager = ContextManager()
    started = time.perf_counter()
    async with open_tool_registry(memory, mcp_servers=servers, include_knowledge=False, context_manager=manager) as registry:
        traced_registry(registry, row, 'initial', fail_fast_missing=name == 'replan')
        runtime = AgentRuntime(Planner(PlanningModel(model), context_manager=manager), model, memory, registry,
                               max_executions=12, max_replans=2, step_timeout_seconds=90,
                               planning_timeout_seconds=60, context_manager=manager)
        first = await runtime.run()
        row['results'].append(asdict(first))
        old_dispatcher = runtime._runner
    row['checks']['first_connection_closed'] = await closed_probe(registry)
    if name == 'pause':
        row['checks']['blocked_before_resume'] = first.status is TaskStatus.BLOCKED
        row['checks']['evidence_completed_before_pause'] = bool(first.steps) and count_reads(row, 'resume.txt') == 1
        questions = [q for q in memory.task_state.scratchpad.open_questions if q.blocking and q.resolved_at is None]
        if first.status is TaskStatus.BLOCKED and len(questions) == 1:
            memory.task_state.resolve_question(questions[0].question_id, 'JSON')
            new_manager = ContextManager()
            async with open_tool_registry(memory, mcp_servers=servers, include_knowledge=False,
                                          context_manager=new_manager) as fresh:
                traced_registry(fresh, row, 'resumed')
                runtime.rebind(Planner(PlanningModel(model), context_manager=new_manager), model, fresh,
                               context_manager=new_manager)
                final = await runtime.run()
                row['results'].append(asdict(final))
            row['checks']['second_connection_closed'] = await closed_probe(fresh)
            row['checks']['new_dispatcher'] = runtime._runner is not old_dispatcher
            row['checks']['same_task'] = final.task_id == first.task_id
            row['checks']['completed_after_resume'] = final.status is TaskStatus.COMPLETED
            row['checks']['no_completed_evidence_replay'] = count_reads(row, 'resume.txt') == 1
            row['checks']['output_matches_evidence'] = read_output(files, 'resume-result.json').get('token') == nonce
            row['checks']['fresh_mcp_used'] = any(e['phase'] == 'resumed' and e['call']['tool_name'] == 'write_file'
                                                 and e.get('result', {}).get('success') for e in row['tool_events'])
        else:
            row['checks']['completed_after_resume'] = False
    elif name == 'multi':
        output = read_output(files, 'report.json')
        row['checks'].update(completed=first.status is TaskStatus.COMPLETED,
            reference_matches_file=output.get('reference') == nonce,
            correct_total=output.get('total') == 106, currency=output.get('currency') == 'CNY',
            timestamp=bool(output.get('timestamp')),
            both_inputs_read=count_reads(row,'metadata.json') >= 1 and count_reads(row,'prices.csv') >= 1,
            output_readback=count_reads(row,'report.json') >= 1,
            clock_called=any(e['call']['tool_name'] == 'get_current_time' for e in row['tool_events']),
            multiple_tools=len(row['tool_events']) >= 5)
    else:
        row['checks'].update(completed=first.status is TaskStatus.COMPLETED,
            runtime_replanned=first.replan_count >= 1,
            real_missing_file=any(e.get('injected_fail_fast') for e in row['tool_events']),
            primary_tried_once=count_reads(row,'primary.txt') == 1,
            fallback_read=count_reads(row,'backup.txt') >= 1,
            fallback_value_persisted=read_output(files,'recovery.json').get('recovery_key') == nonce,
            output_readback=count_reads(row,'recovery.json') >= 1)
    row['routes'] = [asdict(x) for x in runtime.routing_decisions]
    row['checks']['existing_runner_only'] = bool(row['routes']) and all(x['runner_id'] == 'tool_calling' for x in row['routes'])
    row['checks']['routing_count_matches_attempts'] = len(row['routes']) == row['results'][-1]['execution_count']
    row['seconds'] = time.perf_counter()-started
    row['passed'] = all(row['checks'].values())
    return row


async def main(args):
    root = Path(args.output or f'tmp/live-multi-runner-{uuid4().hex[:8]}').resolve()
    root.mkdir(parents=True, exist_ok=False)
    report = dict(model=config.rag_model, source_root=str(Path(__file__).resolve().parents[1]),
                  fixture_root=str(root), scenarios=[], model_events=[])
    async with AsyncOpenAI(api_key=config.dashscope_api_key, base_url=config.dashscope_base_url,
                           timeout=45, max_retries=0) as client:
        adapter = OpenAICompatibleToolCallingModel(client, config.rag_model, temperature=0,
            json_mode=config.rag_json_mode, enable_thinking=config.rag_enable_thinking,
            max_output_tokens=config.context_output_tokens)
        model = RecordedModel(adapter, report['model_events'])
        for name in ('multi', 'pause', 'replan'):
            if args.case != 'all' and args.case != name:
                continue
            try:
                async with asyncio.timeout(240):
                    row = await run_case(root, name, model)
            except Exception as exc:
                row = dict(case=name, passed=False, error=type(exc).__name__)
            report['scenarios'].append(row)
            (root/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, default=encode), encoding='utf-8')
            print(json.dumps({k: row.get(k) for k in ('case','passed','checks','error','seconds')}), flush=True)
    report['passed'] = all(x['passed'] for x in report['scenarios'])
    (root/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, default=encode), encoding='utf-8')
    print(json.dumps({'report':str(root/'report.json'),'passed':report['passed'],'model_calls':len(report['model_events'])}), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--case', choices=('all','multi','pause','replan'), default='all')
    parser.add_argument('--output')
    args = parser.parse_args()
    if not args.live:
        parser.error('--live is required; no API calls made')
    raise SystemExit(asyncio.run(main(args)))
