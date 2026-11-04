"""Opt-in synthetic live validation; never modifies configured model defaults."""
import argparse
import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from functools import partial
import json
from pathlib import Path
import re
import sys
import time
from uuid import uuid4
from unittest.mock import patch
from loguru import logger

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from openai import AsyncOpenAI
from app.config import config
from app.core.dependencies import conversation_compressor, ollama_client
from app.agent.json_output import load_json
from app.agent.openai_compatible_tool_calling_model import OpenAICompatibleToolCallingModel
from app.models.conversation_memory import ConversationMemory, ConversationMessage, ConversationTurn, MessageRole, TurnStatus
from app.services.conversation_archive import ConversationArchive
from app.services.context_manager import ContextManager
from app.services.memory_compression.llm_compressor import LLMCompressor
from app.services.rag_agent_service import RagAgentService
from app.tools.tool_registry import ToolRegistry
import app.services.rag_agent_service as service_module


CASES = [
    dict(name='北辰', facts=[
        ('completed', '北辰数据库选用PostgreSQL，端口6432。禁止使用SQLite。', '已记录数据库决策；没有执行迁移。'),
        ('failed', '北辰迁移尝试失败，不能记为迁移成功。', '迁移失败，数据库决策不变。'),
        ('blocked', '北辰发布负责人尚未确定，必须等用户确认。', '请确认发布负责人。'),
    ], expected={'database': 'PostgreSQL', 'port': 6432, 'sqlite_allowed': False,
                 'migration_succeeded': False, 'owner': 'unknown'},
        checks=[r'PostgreSQL', r'6432', r'(?:禁止|不允许|不得|不能|不可).{0,10}SQLite|SQLite.{0,8}(?:禁止|不允许|不可|不得)',
                r'失败|未成功', r'未确定|待确认|未确认|尚未|待用户']),
    dict(name='海岚', facts=[
        ('completed', '海岚原计划3个副本，现已明确改为5个副本。禁止重启服务。', '最终副本目标为5，不再是3；不允许重启。'),
        ('cancelled', '海岚发布操作已取消，不能说发布已经完成。', '发布已取消，没有完成发布。'),
    ], expected={'replicas': 5, 'restart_allowed': False, 'rollout_completed': False},
        checks=[r'5|五', r'禁止.{0,8}重启|不.{0,5}重启', r'取消', r'未完成|没有完成|不能.{0,12}完成|不.{0,5}完成']),
    dict(name='星桥', facts=[
        ('completed', '星桥交付文档必须使用中文并标明引用来源。只有只读访问权限，不允许写入。', '已记录文档要求及只读权限。'),
        ('blocked', '星桥审批仍在等待中，没有获得批准。', '等待审批，不得假定已经批准。'),
    ], expected={'language': '中文', 'quote_source_required': True, 'approval': 'pending', 'write_allowed': False},
        checks=[r'中文', r'引用|来源', r'只读|不允许写|禁止写', r'等待|待审批|未批准|未获得批准|未获批准']),
]


def contains_fact(text, value):
    """Do not mistake a digit inside timestamps/IDs for a numeric fact."""
    if isinstance(value, bool) or value == 'unknown':
        return False
    if isinstance(value, int):
        return bool(re.search(r'(?<![0-9A-Za-z_])' + re.escape(str(value)) + r'(?![0-9A-Za-z_])', text))
    return isinstance(value, str) and value in text


@asynccontextmanager
async def context_only_registry(*args, **kwargs):
    """A/B isolation: neither arm may recover facts via artifact tools."""
    yield ToolRegistry()


def pinned_history(messages):
    histories = []
    for message in messages:
        text = getattr(message, 'content', None)
        if not isinstance(text, str):
            continue
        start = text.find('\n{') + 1 if not text.lstrip().startswith('{') else len(text)-len(text.lstrip())
        try:
            data, _ = json.JSONDecoder().raw_decode(text[start:])
            for pin in data.get('pinned_contexts', []):
                if pin.get('source') == 'conversation_memory':
                    histories.append(json.loads(pin['content']))
        except (ValueError, TypeError, AttributeError):
            continue
    return histories


class RecordedModel:
    def __init__(self, model):
        self.model = model
        self.calls = []

    async def generate(self, messages, tools):
        histories = pinned_history(messages)
        event = dict(body_counts=[len(h.get('summary_bodies', [])) for h in histories],
                     request_messages=[asdict(m) for m in messages])
        start = time.perf_counter()
        try:
            result = await self.model.generate(messages, tools)
            event['response'] = asdict(result)
            return result
        finally:
            event['seconds'] = time.perf_counter()-start
            self.calls.append(event)


async def run(args):
    root = Path(args.output or f'tmp/live-summary-context-20261031-{uuid4().hex[:8]}')
    root.mkdir(parents=True, exist_ok=False)
    summary_model = conversation_compressor._model if args.summary_provider == 'local' else config.rag_model
    report = dict(main_model=config.rag_model, summary_model=summary_model, summary_provider=args.summary_provider,
                  artifact_tools_enabled=False,
                  rounds=args.rounds, cases=[], failures=[])
    original_manager = service_module.ContextManager
    async with AsyncOpenAI(base_url=ollama_client.base_url, api_key=ollama_client.api_key,
                           timeout=60, max_retries=0) as local, AsyncOpenAI(
        base_url=config.dashscope_base_url, api_key=config.dashscope_api_key, timeout=45, max_retries=0,
    ) as remote:
        remote_create = remote.chat.completions.create
        async def configured_create(**kwargs):
            extra = dict(kwargs.get('extra_body') or {})
            extra.setdefault('enable_thinking', config.rag_enable_thinking)
            return await remote_create(**(kwargs | {'extra_body': extra}))
        # Runtime-only injection: match the configured main-model thinking
        # setting, without editing defaults or the local compressor adapter.
        remote.chat.completions.create = configured_create
        compressor = LLMCompressor(local if args.summary_provider == 'local' else remote, summary_model)
        for trial in range(args.rounds):
            for case_index, case in enumerate(CASES):
                if args.case != 'all' and args.case != ('beichen', 'hailan', 'xingqiao')[case_index]:
                    continue
                row = dict(case=case['name'], trial=trial, runs=[])
                report['cases'].append(row)
                archive_root = root / f'{trial}-{case["name"]}'
                archive = ConversationArchive(archive_root, 's')
                source_ids = []
                for index, (status, user, answer) in enumerate(case['facts']):
                    stamp = datetime.now(timezone.utc)
                    # Put facts beyond raw_index's preview, so it cannot leak
                    # the full answer and masquerade as summary-body use.
                    text = case['name'] + '历史记录。' + '原始材料。' * 60 + user
                    turn = ConversationTurn([
                        ConversationMessage(MessageRole.USER, text, stamp),
                        ConversationMessage(MessageRole.ASSISTANT, answer, stamp),
                    ], f'{case["name"]}-{index}', stamp, task_id='historical-task',
                       status=TurnStatus(status), finished_at=stamp)
                    archive.save_turn(turn)
                    source_ids.append(turn.turn_id)
                for i in range(22):
                    archive.save_turn(ConversationTurn([ConversationMessage(MessageRole.USER, '近期无关中性对话')]))
                memory = ConversationMemory('s', compressor=compressor, archive=archive)
                await memory.restore()
                start = time.perf_counter()
                try:
                    # Expose exceptions in validation instead of logging a
                    # failed compressor invocation as a successful experiment.
                    assert await memory.compress_pending(force=True, raise_errors=True)
                    entry = memory.summary_index[-1]
                    summary = archive.read_summary(entry.memory_id)
                    row.update(summary=summary, summary_seconds=time.perf_counter()-start,
                               source_turn_ids=list(entry.source_turn_ids),
                               summary_checks=[bool(re.search(p, summary)) for p in case['checks']])
                    if not all(row['summary_checks']):
                        report['failures'].append(f'{trial}/{case["name"]}: summary fact/uncertainty probe')
                except Exception as exc:
                    row.update(summary_error=type(exc).__name__, summary_seconds=time.perf_counter()-start)
                    report['failures'].append(f'{trial}/{case["name"]}: summary {type(exc).__name__}')
                    (root/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
                    print(json.dumps({k: v for k, v in row.items() if k != 'summary'}, ensure_ascii=False), flush=True)
                    continue
                for enabled in (True, False):
                    # Independent archives plus neutral index metadata isolate
                    # body recall from earlier answers and description previews.
                    run_root = root / f'{trial}-{case["name"]}-bodies-{enabled}'
                    run_archive = ConversationArchive(run_root, 's')
                    for source in archive.load_turns():
                        run_archive.save_turn(source)
                    run_archive.save_summary(entry.source_turn_ids, summary, case['name']+'历史摘要',
                                             '带来源的历史说明；具体决策见正文。')
                    service_module.ContextManager = partial(original_manager, summary_body_tokens=2048 if enabled else 0)
                    adapter = OpenAICompatibleToolCallingModel(remote, config.rag_model, temperature=0,
                        json_mode=config.rag_json_mode, enable_thinking=config.rag_enable_thinking,
                        max_output_tokens=config.context_output_tokens)
                    recorded = RecordedModel(adapter)
                    question = (
                        f'仅根据已经提供的{case["name"]}历史上下文作答，不调用任何工具，不搜索原文。'
                        '只需一个回答步骤，不需要实际执行任何发布或迁移。没有证据时字段值写字符串unknown，禁止猜测。'
                        '回答content内必须为一个JSON对象，字段和类型如下：'
                        + json.dumps({key: ('boolean' if isinstance(value, bool) else 'integer' if isinstance(value, int) else 'string')
                                      for key, value in case['expected'].items()}, ensure_ascii=False)
                        + '。必须恰好包含这些字段。'
                        + ('approval只能用pending/approved/unknown。' if 'approval' in case['expected'] else '')
                        + ('owner若未确定必须unknown。' if 'owner' in case['expected'] else '')
                    )
                    start = time.perf_counter()
                    event = dict(bodies_enabled=enabled)
                    try:
                        projection_memory = ConversationMemory('s', archive=run_archive)
                        await projection_memory.restore()
                        prep_start = time.perf_counter()
                        preview = json.loads(await service_module.ContextManager().prepare_conversation(projection_memory, question))
                        event['context_prepare_ms'] = (time.perf_counter()-prep_start)*1000
                        event['preflight_body_count'] = len(preview['summary_bodies'])
                        assert bool(preview['summary_bodies']) == enabled
                        neutral = json.dumps(preview, ensure_ascii=False)
                        if not enabled:
                            # Specific source facts must not be supplied by raw
                            # previews/index metadata in the disabled control.
                            leaked = [value for value in case['expected'].values() if contains_fact(neutral, value)]
                            assert not leaked, f'control context contains target facts: {leaked}'
                        async with RagAgentService(model=recorded, mcp_servers=(), include_knowledge=False,
                                                    memory_directory=run_root, max_replans=0,
                                                    step_timeout_seconds=45, planning_timeout_seconds=45) as service:
                            result = await service.query_result(question, 's', request_id=f'live-{enabled}')
                        event.update(status=result.status.value, answer=result.content,
                                     executions=result.execution_count, replans=result.replan_count)
                        try:
                            parsed = load_json(result.content)
                            event['answer_is_single_json'] = True
                        except Exception:
                            event['answer_is_single_json'] = False
                            event['last_step_answer'] = result.steps[-1].result_summary if result.steps else ''
                            try:
                                parsed = load_json(event['last_step_answer'])
                            except Exception:
                                parsed = {}
                        counts = [count for call in recorded.calls for count in call['body_counts']]
                        event['body_reached_models'] = bool(counts) and all(bool(n) == enabled for n in counts)
                        event['tool_call_count'] = sum(len(call.get('response', {}).get('tool_calls', []))
                                                       for call in recorded.calls)
                        if event['tool_call_count']:
                            report['failures'].append(f'{trial}/{case["name"]}/{enabled}: unexpected tool use')
                        if not event['body_reached_models']:
                            report['failures'].append(f'{trial}/{case["name"]}/{enabled}: projection gate')
                        event['checks'] = {key: parsed.get(key) == value for key, value in case['expected'].items()}
                        event['all_correct'] = all(event['checks'].values())
                        if enabled and (result.status.value != 'completed' or not event['all_correct']):
                            report['failures'].append(f'{trial}/{case["name"]}: body-enabled Agent answer')
                    except Exception as exc:
                        event['error'] = type(exc).__name__
                        report['failures'].append(f'{trial}/{case["name"]}/{enabled}: {type(exc).__name__}')
                    finally:
                        event['seconds'] = time.perf_counter()-start
                        event['model_calls'] = recorded.calls
                        row['runs'].append(event)
                        service_module.ContextManager = original_manager
                (root/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
                print(json.dumps({'case': case['name'], 'trial': trial, 'summary_seconds':row.get('summary_seconds'),
                    'summary_checks':row.get('summary_checks'), 'runs':[
                        {k:v for k,v in r.items() if k not in ('model_calls',)} for r in row['runs']]}, ensure_ascii=False), flush=True)
    (root/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'report':str(root/'report.json'), 'failures':report['failures']}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Explicitly permit real model calls on synthetic fixtures')
    parser.add_argument('--rounds', type=int, choices=range(1, 4), default=1)
    parser.add_argument('--summary-provider', choices=('local', 'remote'), default='local')
    parser.add_argument('--case', choices=('all', 'beichen', 'hailan', 'xingqiao'), default='all')
    parser.add_argument('--output')
    args = parser.parse_args()
    if not args.live:
        parser.error('--live is required; no API calls were made')
    logger.remove()
    def no_trace(record):
        record['exception'] = None
        return True
    logger.add(sys.stderr, format='{level}: {message}', backtrace=False, diagnose=False, filter=no_trace)
    with patch.object(service_module, 'open_tool_registry', context_only_registry):
        asyncio.run(run(args))
