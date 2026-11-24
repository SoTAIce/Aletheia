"""Read-only batch runner: python -m app.agent.batch MANIFEST --out OUT --max-tokens N (D-097)."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time

from app.agent.__main__ import _notice_to, _positive, load_output_schema, open_cli_service
from app.agent.run_meter import RunMeter, metering
from app.config import config
from app.models.taskstate import TaskStatus
from app.services.workspace import WorkspacePolicy

DEFAULT_ITEM_MAX_TOKENS = 200_000
DEFAULT_ITEM_MAX_SECONDS = 300.0
MAX_ID_CHARS = 100
MAX_EACH_FILE_ITEMS = 500

STATUS_LABELS = {
    'completed': '完成',
    'failed': '失败',
    'blocked': '阻塞',
    'cancelled': '已取消',
    'not_run': '未运行',
}


class BatchError(ValueError):
    """User-facing batch configuration or input error (Chinese message)."""


@dataclass(frozen=True, slots=True)
class BatchItem:
    id: str
    goal: str
    output_schema: dict | None = None
    output_schema_sha256: str | None = None


def _load_schema_with_hash(path: Path) -> tuple[dict, str]:
    """Load and validate a UTF-8 JSON Schema file, returning (schema, sha256_hex)."""
    try:
        schema = load_output_schema(str(path))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except ValueError as exc:
        raise BatchError(str(exc)) from exc
    except OSError as exc:
        raise BatchError(f'无法读取 Schema 文件：{type(exc).__name__}') from exc
    return schema, digest


def load_manifest(path: str | Path) -> list[BatchItem]:
    """Parse a JSONL manifest into validated BatchItems."""
    manifest_path = Path(path)
    try:
        text = manifest_path.read_text(encoding='utf-8')
    except OSError as exc:
        raise BatchError(f'无法读取清单文件：{type(exc).__name__}') from exc

    items: list[BatchItem] = []
    seen_ids: set[str] = set()
    for lineno, raw_line in enumerate(text.splitlines(), 1):
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise BatchError(f'清单第 {lineno} 行不是合法的 JSON') from exc
        if not isinstance(obj, dict):
            raise BatchError(f'清单第 {lineno} 行必须是 JSON 对象')

        item_id = obj.get('id')
        if not isinstance(item_id, str) or not item_id.strip():
            raise BatchError(f'清单第 {lineno} 行缺少非空字符串 id')
        item_id = item_id.strip()
        if len(item_id) > MAX_ID_CHARS:
            raise BatchError(f'清单第 {lineno} 行的 id 超过 {MAX_ID_CHARS} 个字符')
        if item_id in seen_ids:
            raise BatchError(f'清单第 {lineno} 行的 id 重复：{item_id}')
        seen_ids.add(item_id)

        goal = obj.get('goal')
        if not isinstance(goal, str) or not goal.strip():
            raise BatchError(f'清单第 {lineno} 行缺少非空字符串 goal')

        schema = None
        schema_hash = None
        raw_schema = obj.get('output_schema')
        if raw_schema is not None:
            if not isinstance(raw_schema, str) or not raw_schema.strip():
                raise BatchError(f'清单第 {lineno} 行的 output_schema 必须是非空文件路径')
            candidate = Path(raw_schema.strip())
            if not candidate.is_absolute() and (manifest_path.parent / candidate).is_file():
                candidate = manifest_path.parent / candidate
            try:
                schema, schema_hash = _load_schema_with_hash(candidate)
            except BatchError as exc:
                raise BatchError(f'清单第 {lineno} 行的 output_schema 无效：{exc}') from exc

        items.append(BatchItem(id=item_id, goal=goal, output_schema=schema, output_schema_sha256=schema_hash))

    if not items:
        raise BatchError('清单为空，没有任何任务项')
    return items


def _inside(path: Path, folder: Path) -> bool:
    return path == folder or folder in path.parents


def expand_each_file(pattern: str, goal_template: str, root: str | Path | None, *,
                     policy: WorkspacePolicy | None = None) -> list[BatchItem]:
    """Expand --each-file GLOB under root into sorted BatchItems, skipping closed folders."""
    if not isinstance(goal_template, str) or not goal_template.strip():
        raise BatchError('使用 --each-file 时必须提供非空的 --goal 模板')
    if '{file}' not in goal_template:
        raise BatchError('使用 --each-file 时，--goal 模板里必须包含 {file}')
    if not root:
        raise BatchError('使用 --each-file 时必须指定 --filesystem-root（或在配置中设置 MCP_FILESYSTEM_ROOT）')
    try:
        root_path = Path(root).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise BatchError(f'工作目录无效或不存在：{type(exc).__name__}') from exc
    if not root_path.is_dir():
        raise BatchError('工作目录不是一个目录')

    closed = (policy or WorkspacePolicy.from_config()).closed_folders()
    try:
        raw_matches = list(root_path.glob(pattern))
    except (ValueError, NotImplementedError, OSError) as exc:
        raise BatchError(f'--each-file 模式无效：{type(exc).__name__}') from exc

    rel_paths: set[str] = set()
    for path in raw_matches:
        try:
            if not path.is_file():
                continue
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if not _inside(resolved, root_path) or resolved == root_path:
            continue
        if any(_inside(resolved, folder) or _inside(path.absolute(), folder) for folder in closed):
            continue
        try:
            rel = path.relative_to(root_path).as_posix()
        except ValueError:
            continue
        rel_paths.add(rel)

    ordered = sorted(rel_paths)
    if not ordered:
        raise BatchError('没有匹配到任何文件')
    if len(ordered) > MAX_EACH_FILE_ITEMS:
        raise BatchError(f'--each-file 匹配到 {len(ordered)} 个文件，超过上限（最多 {MAX_EACH_FILE_ITEMS} 项）')
    return [BatchItem(id=rel, goal=goal_template.replace('{file}', rel)) for rel in ordered]


def compute_batch_id(items: Sequence[BatchItem], batch_schema_sha256: str | None = None) -> str:
    """First 12 hex chars of SHA-256 over canonical expanded items + batch output schema digest."""
    payload = {
        'items': [
            {'id': item.id, 'goal': item.goal, 'output_schema_sha256': item.output_schema_sha256}
            for item in items
        ],
        'output_schema_sha256': batch_schema_sha256,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:12]


def read_results(path: Path, expected_batch_id: str, *, write=print) -> tuple[dict[str, dict], int | None]:
    """Read an existing batch JSONL result file for resume.

    Returns (latest_record_by_id, truncate_offset_or_None). Raises BatchError on mismatch
    without modifying the file. Ignores an incomplete trailing JSON line with a notice.
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise BatchError(f'无法读取已有结果文件：{type(exc).__name__}') from exc

    lines: list[tuple[int, bytes]] = []
    offset = 0
    for chunk in raw.splitlines(keepends=True):
        if chunk.strip():
            lines.append((offset, chunk))
        offset += len(chunk)

    if not lines:
        raise BatchError('已有结果文件为空，无法续跑，未修改结果文件')

    try:
        header = json.loads(lines[0][1].decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BatchError('已有结果文件的批次头损坏，无法续跑，未修改结果文件') from exc
    if not isinstance(header, dict) or header.get('type') != 'batch' or not isinstance(header.get('batch_id'), str):
        raise BatchError('已有结果文件缺少合法的批次头，无法续跑，未修改结果文件')
    if header['batch_id'] != expected_batch_id:
        raise BatchError(
            f"结果文件的批次编号（{header['batch_id']}）与当前清单（{expected_batch_id}）不一致，"
            f'已停止，未修改结果文件'
        )

    prior: dict[str, dict] = {}
    truncate_to: int | None = None
    last_idx = len(lines) - 1
    for idx in range(1, len(lines)):
        line_offset, chunk = lines[idx]
        try:
            record = json.loads(chunk.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            if idx == last_idx:
                write('提示：结果文件最后一行不是完整 JSON，已忽略并将重跑该项。')
                truncate_to = line_offset
                break
            raise BatchError(f'结果文件第 {idx + 1} 行损坏，无法续跑，未修改结果文件') from exc
        if not isinstance(record, dict):
            if idx == last_idx:
                write('提示：结果文件最后一行不是完整 JSON，已忽略并将重跑该项。')
                truncate_to = line_offset
                break
            raise BatchError(f'结果文件第 {idx + 1} 行不是 JSON 对象，无法续跑，未修改结果文件')
        if record.get('type') == 'item' and isinstance(record.get('id'), str):
            prior[record['id']] = record

    return prior, truncate_to


def confirm_batch(total: int, to_run: int, max_tokens: int, item_max_tokens: int,
                  item_max_seconds: float, *, confirmed: bool, write=print, ask=input,
                  interactive: bool | None = None) -> str | None:
    """Print batch size and budget, then ask for 'y' unless --confirm-batch is given."""
    write(f'批处理共 {total} 项，本次运行 {to_run} 项；整批令牌上限 {max_tokens:,}，'
          f'每项上限 {item_max_tokens:,} 令牌 / {item_max_seconds:g} 秒。')
    if confirmed:
        return None
    is_tty = sys.stdin.isatty() if interactive is None else interactive
    if not is_tty:
        return '当前不是交互终端，无法确认批处理；确认要运行时请加 --confirm-batch。'
    try:
        answer = ask('输入 y 开始，其他任意输入取消：')
    except EOFError:
        answer = ''
    if answer.strip().lower() != 'y':
        return '已取消批处理。'
    return None


def _append_jsonl(fp, record: dict) -> None:
    fp.write(json.dumps(record, ensure_ascii=False) + '\n')
    fp.flush()
    os.fsync(fp.fileno())


def _usage_tokens(usage, meter: RunMeter | None = None) -> int:
    if isinstance(usage, dict):
        value = usage.get('total_tokens')
        if type(value) is int and value >= 0:
            return value
    return meter.total_tokens if meter is not None else 0


def _usage_seconds(usage, meter: RunMeter | None = None) -> float:
    if isinstance(usage, dict):
        value = usage.get('seconds')
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return round(float(value), 3)
    return round(meter.elapsed(), 3) if meter is not None else 0.0


def _item_record_from_result(batch_id: str, item_id: str, result, meter: RunMeter) -> dict:
    usage = result.usage if isinstance(result.usage, dict) else {}
    tokens = _usage_tokens(usage, meter)
    seconds = _usage_seconds(usage, meter)
    record = {
        'type': 'item',
        'batch_id': batch_id,
        'id': item_id,
        'status': result.status.value,
        'failure_kind': result.failure_kind,
        'answer': result.content,
        'tokens': tokens,
        'seconds': seconds,
        'usage': usage,
        'task_id': result.task_id,
    }
    output = getattr(result, 'output', None)
    has_contract = 'contract' in usage
    if output is not None or (has_contract and result.status is TaskStatus.COMPLETED):
        record['output'] = output
    return record


def _synthetic_item_record(batch_id: str, item_id: str, status: str, failure_kind: str | None,
                           meter: RunMeter | None = None) -> dict:
    usage = meter.snapshot() if meter is not None else {}
    return {
        'type': 'item',
        'batch_id': batch_id,
        'id': item_id,
        'status': status,
        'failure_kind': failure_kind,
        'answer': '',
        'tokens': _usage_tokens(usage, meter),
        'seconds': _usage_seconds(usage, meter),
        'usage': usage,
        'task_id': None,
    }


def _summarize(items: Sequence[BatchItem], latest: dict[str, dict], total_tokens: int, elapsed: float) -> tuple[str, int]:
    counts = {'completed': 0, 'failed': 0, 'blocked': 0, 'cancelled': 0, 'not_run': 0}
    for item in items:
        rec = latest.get(item.id)
        status = rec.get('status') if isinstance(rec, dict) else 'not_run'
        if status in counts:
            counts[status] += 1
        else:
            counts['failed'] += 1
    parts = [f"完成 {counts['completed']}", f"失败 {counts['failed']}"]
    if counts['blocked']:
        parts.append(f"阻塞 {counts['blocked']}")
    if counts['cancelled']:
        parts.append(f"已取消 {counts['cancelled']}")
    parts.append(f"未运行 {counts['not_run']}")
    line = '，'.join(parts) + f'；令牌 {total_tokens}，耗时 {round(elapsed, 1):g} 秒'
    if counts['not_run'] > 0:
        code = 2
    elif counts['failed'] > 0 or counts['blocked'] > 0 or counts['cancelled'] > 0:
        code = 1
    else:
        code = 0
    return line, code


def _service_args(args) -> argparse.Namespace:
    return argparse.Namespace(
        filesystem_root=getattr(args, 'filesystem_root', None),
        write_root=None,
        sandbox=False,
        execution_mode='single',
        delegate=False,
        compress_history=False,
        team_timeout=None,
        single_runner='loop',
        no_knowledge=getattr(args, 'no_knowledge', False),
        persona=getattr(args, 'persona', None),
        trust_instructions=getattr(args, 'trust_instructions', False),
        no_instructions=getattr(args, 'no_instructions', False),
        confirm_system_drive=getattr(args, 'confirm_system_drive', False),
        confirm_mcp=getattr(args, 'confirm_mcp', False),
        max_executions=getattr(args, 'max_executions', 16),
        user_id=None,
    )


async def run_batch(args, *, write=print, ask=input, interactive: bool | None = None,
                    service_opener=None) -> int:
    """Execute one read-only batch run and return its exit code (0, 1, 2, or 130)."""
    batch_schema = None
    batch_schema_hash = None
    if getattr(args, 'output_schema', None):
        try:
            batch_schema, batch_schema_hash = _load_schema_with_hash(Path(args.output_schema))
        except BatchError as exc:
            write(f'OutputSchemaError: {exc}')
            return 2

    try:
        if getattr(args, 'manifest', None):
            items = load_manifest(args.manifest)
            manifest_label = str(args.manifest)
        else:
            root = getattr(args, 'filesystem_root', None) or config.mcp_filesystem_root or None
            items = expand_each_file(args.each_file, args.goal, root)
            manifest_label = f'--each-file:{args.each_file}'
    except BatchError as exc:
        write(f'BatchError: {exc}')
        return 2

    batch_id = compute_batch_id(items, batch_schema_hash)
    out_path = Path(args.out)
    resuming = out_path.exists()
    prior: dict[str, dict] = {}
    truncate_to: int | None = None
    if resuming:
        try:
            prior, truncate_to = read_results(out_path, batch_id, write=write)
        except BatchError as exc:
            write(f'BatchError: {exc}')
            return 2

    skip_statuses = {'completed', 'failed'} if getattr(args, 'skip_failed', False) else {'completed'}
    skipped_ids = {
        item.id for item in items
        if isinstance(prior.get(item.id), dict) and prior[item.id].get('status') in skip_statuses
    }
    to_run = len(items) - len(skipped_ids)
    latest = dict(prior)

    if to_run == 0:
        write('所有项均已完成或跳过，无需运行。')
        summary, code = _summarize(items, latest, 0, 0.0)
        write(summary)
        return code

    if config.mcp_filesystem_write_root:
        write('批处理只读，不会使用写入目录')

    problem = confirm_batch(
        len(items), to_run, args.max_tokens, args.item_max_tokens, args.item_max_seconds,
        confirmed=getattr(args, 'confirm_batch', False),
        write=write, ask=ask, interactive=interactive,
    )
    if problem:
        write(problem)
        return 2

    opener = service_opener or open_cli_service
    svc_args = _service_args(args)
    started = time.monotonic()
    used_tokens = 0
    interrupted = False

    async with AsyncExitStack() as stack:
        opened = await opener(svc_args, stack)
        if isinstance(opened, int):
            return opened
        service, persona_id = opened.service, opened.persona_id

        out_path.parent.mkdir(parents=True, exist_ok=True)
        if resuming:
            if truncate_to is not None:
                with out_path.open('r+b') as raw_fp:
                    raw_fp.truncate(truncate_to)
                    raw_fp.flush()
                    os.fsync(raw_fp.fileno())
            with out_path.open('rb') as check_fp:
                content = check_fp.read()
                needs_newline = bool(content) and not content.endswith(b'\n')
            fp = out_path.open('a', encoding='utf-8', newline='\n')
            if needs_newline:
                fp.write('\n')
                fp.flush()
                os.fsync(fp.fileno())
        else:
            fp = out_path.open('w', encoding='utf-8', newline='\n')
            header = {
                'type': 'batch',
                'batch_id': batch_id,
                'manifest': manifest_label,
                'items': len(items),
                'started_at': datetime.now(timezone.utc).isoformat(),
                'options': {
                    'max_tokens': args.max_tokens,
                    'max_seconds': getattr(args, 'max_seconds', None),
                    'item_max_tokens': args.item_max_tokens,
                    'item_max_seconds': args.item_max_seconds,
                    'no_knowledge': bool(getattr(args, 'no_knowledge', False)),
                    'max_executions': args.max_executions,
                    'skip_failed': bool(getattr(args, 'skip_failed', False)),
                    'has_output_schema': batch_schema is not None,
                },
            }
            _append_jsonl(fp, header)

        total = len(items)
        try:
            for index, item in enumerate(items, 1):
                if item.id in skipped_ids:
                    continue

                remaining_tokens = args.max_tokens - used_tokens
                max_seconds = getattr(args, 'max_seconds', None)
                remaining_seconds = None if max_seconds is None else max_seconds - (time.monotonic() - started)

                if remaining_tokens <= 0 or (remaining_seconds is not None and remaining_seconds <= 0):
                    rec = _synthetic_item_record(batch_id, item.id, 'not_run', 'budget')
                    _append_jsonl(fp, rec)
                    latest[item.id] = rec
                    write(f'[{index}/{total}] {item.id} {STATUS_LABELS["not_run"]}，令牌 0')
                    continue

                effective_tokens = min(args.item_max_tokens, remaining_tokens)
                effective_seconds = (
                    args.item_max_seconds if remaining_seconds is None
                    else min(args.item_max_seconds, remaining_seconds)
                )
                meter = RunMeter(max_total_tokens=effective_tokens, timeout_seconds=effective_seconds)
                session_id = f'batch-{batch_id}-{index}'
                extra = {
                    'allow_writes': False,
                    'use_memory': False,
                    'execution_mode': 'single',
                    'allow_ask_user': False,
                    'on_event': _notice_to(write),
                }
                if persona_id is not None:
                    extra['persona'] = persona_id
                item_schema = item.output_schema if item.output_schema is not None else batch_schema
                if item_schema is not None:
                    extra['output_schema'] = item_schema

                try:
                    with metering(meter):
                        result = await service.query_result(
                            item.goal, session_id, user_id=svc_args.user_id, **extra,
                        )
                except (KeyboardInterrupt, asyncio.CancelledError):
                    rec = _synthetic_item_record(batch_id, item.id, 'cancelled', 'cancelled', meter)
                    _append_jsonl(fp, rec)
                    latest[item.id] = rec
                    used_tokens += rec['tokens']
                    write(f'[{index}/{total}] {item.id} {STATUS_LABELS["cancelled"]}，令牌 {rec["tokens"]}')
                    interrupted = True
                    break
                except Exception as exc:
                    rec = _synthetic_item_record(batch_id, item.id, 'failed', type(exc).__name__, meter)
                    _append_jsonl(fp, rec)
                    latest[item.id] = rec
                    used_tokens += rec['tokens']
                    write(f'[{index}/{total}] {item.id} {STATUS_LABELS["failed"]}，令牌 {rec["tokens"]}')
                    continue

                rec = _item_record_from_result(batch_id, item.id, result, meter)
                _append_jsonl(fp, rec)
                latest[item.id] = rec
                used_tokens += rec['tokens']
                label = STATUS_LABELS.get(rec['status'], rec['status'])
                write(f'[{index}/{total}] {item.id} {label}，令牌 {rec["tokens"]}')
        finally:
            fp.close()

    elapsed = time.monotonic() - started
    summary, code = _summarize(items, latest, used_tokens, elapsed)
    write(summary)
    return 130 if interrupted else code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='python -m app.agent.batch',
        description='Aletheia batch runner: read-only sequential batch execution (D-097)',
    )
    parser.add_argument('manifest', nargs='?', help='JSONL manifest file (one JSON object per line)')
    parser.add_argument('--each-file', help='Glob pattern relative to --filesystem-root to expand into items')
    parser.add_argument('--goal', help='Goal template containing {file} when using --each-file')
    parser.add_argument('--out', required=True, help='Output JSONL file path (resumes if it already exists)')
    parser.add_argument('--max-tokens', required=True, type=_positive(int),
                        help='Total token budget for the whole batch (required)')
    parser.add_argument('--max-seconds', type=_positive(float),
                        help='Total time budget for the whole batch in seconds')
    parser.add_argument('--item-max-tokens', type=_positive(int), default=DEFAULT_ITEM_MAX_TOKENS,
                        help=f'Token budget per item (default {DEFAULT_ITEM_MAX_TOKENS})')
    parser.add_argument('--item-max-seconds', type=_positive(float), default=DEFAULT_ITEM_MAX_SECONDS,
                        help=f'Time budget per item in seconds (default {DEFAULT_ITEM_MAX_SECONDS:g})')
    parser.add_argument('--skip-failed', action='store_true',
                        help='When resuming, skip previously failed items as well as completed ones')
    parser.add_argument('--confirm-batch', action='store_true',
                        help='Confirm running the batch without an interactive prompt')
    parser.add_argument('--filesystem-root', help='Directory exposed by the filesystem MCP server')
    parser.add_argument('--no-knowledge', action='store_true',
                        help='Disable RAG when no knowledge store is configured')
    parser.add_argument('--output-schema', help='UTF-8 JSON Schema file that each final answer must match')
    parser.add_argument('--persona', help='Persona (name or id) for every item in the batch')
    parser.add_argument('--trust-instructions', action='store_true',
                        help="Trust the working directory's project instructions file for this run")
    parser.add_argument('--no-instructions', action='store_true',
                        help='Do not read project instructions files from the working directory')
    parser.add_argument('--confirm-system-drive', action='store_true',
                        help='Confirm a --filesystem-root on the system drive (C:) without a prompt')
    parser.add_argument('--confirm-mcp', action='store_true',
                        help='Confirm user MCP servers from MCP_SERVERS_FILE without a prompt')
    parser.add_argument('--max-executions', type=_positive(int), default=16)
    return parser


def parse_args(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.manifest and args.each_file:
        parser.error('manifest and --each-file cannot be used together')
    if not args.manifest and not args.each_file:
        parser.error('either a manifest file or --each-file is required')
    if args.each_file and not args.goal:
        parser.error('--each-file requires --goal')
    if args.goal and not args.each_file:
        parser.error('--goal can only be used with --each-file')
    if args.trust_instructions and args.no_instructions:
        parser.error('--trust-instructions and --no-instructions cannot be used together')
    return args


if __name__ == '__main__':
    from app.core.logging_setup import configure_logging
    configure_logging()
    try:
        raise SystemExit(asyncio.run(run_batch(parse_args())))
    except KeyboardInterrupt:
        raise SystemExit(130)
