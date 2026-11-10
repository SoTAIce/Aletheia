"""Read a run's trace on the command line (trace-001, D-073). Read-only: it opens no model, MCP
server or database connection.

    python -m app.agent.trace FILE.jsonl                 one audit or trace file as a text tree
    python -m app.agent.trace --session NAME             the runs archived for this session
    python -m app.agent.trace --session NAME --task ID   the trees of one task's runs
    --user-id ID (or --web for the web page's user) picks a user's sessions; --json prints
    build_trace's result instead of text.

Exit codes: 0 found, 1 nothing found, 2 bad arguments or an unreadable file.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

from app.agent.trace_view import build_trace, render_text

_NAME = re.compile(r'[0-9A-Za-z_-]{1,120}')
MAX_FILE_BYTES = 8 * 1024 * 1024


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='python -m app.agent.trace',
                                     description='查看一次运行的执行过程（追踪）。只读。')
    parser.add_argument('file', nargs='?', help='一个审计或追踪文件（.jsonl）')
    parser.add_argument('--session', help='会话名：从对话归档里找')
    parser.add_argument('--task', help='任务编号：只看这个任务的运行')
    who = parser.add_mutually_exclusive_group()
    who.add_argument('--user-id', help='用户编号（命令行开启长期记忆时是 local）')
    who.add_argument('--web', action='store_true', help='使用网页的用户编号（WEB_USER_ID）')
    parser.add_argument('--memory-dir', help='对话归档目录，默认用配置里的目录')
    parser.add_argument('--json', action='store_true', help='输出 JSON')
    return parser


def read_records(path: Path) -> list[dict]:
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError('文件太大')
    records = []
    for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _archive(args):
    from app.config import config
    from app.services.conversation_archive import ConversationArchive
    from app.services.rag_agent_service import RagAgentService
    user_id = config.web_user_id if args.web else args.user_id
    root = Path(args.memory_dir or config.conversation_storage_dir)
    if user_id is not None:
        root = root / 'user-scoped'
    label = RagAgentService._archive_label(user_id, args.session)
    if not ConversationArchive.exists(root, label):
        return None
    return ConversationArchive(root, label)


def _summary(name: str, trace: dict) -> str:
    seconds = '—' if trace['seconds'] is None else f"{trace['seconds']:.1f} 秒"
    return '  '.join([str(trace.get('started_at') or name), str(trace.get('task_id') or '（无编号）'),
                      str(trace['status']), seconds, f"模型 {trace['totals']['model_calls']} 次",
                      f"工具 {trace['totals']['tool_calls']} 次"])


def main(argv=None, write=print) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 2
    if bool(args.file) == bool(args.session) or (args.task and not args.session):
        parser.print_usage(sys.stderr)
        write('请给一个文件，或者用 --session 指定会话（--task 要和 --session 一起用）。')
        return 2
    if args.file:
        path = Path(args.file)
        if not path.is_file():
            write(f'没有这个文件：{path}')
            return 1
        try:
            traces = [build_trace(read_records(path))]
        except (OSError, ValueError) as exc:
            write(f'读不了这个文件：{type(exc).__name__}')
            return 2
    else:
        archive = _archive(args)
        names = archive.list_audits() if archive is not None else []
        if args.task is not None:
            suffix = '-' + (re.sub(r'[^0-9A-Za-z_-]', '', args.task)[:64] or 'task')
            names = [name for name in names if name.endswith(suffix)]
        pairs = []
        for name in names:
            try:
                records, skipped = archive.load_audit(name)
            except (KeyError, ValueError, OSError):
                continue
            trace = build_trace(records)
            if args.task is not None and trace.get('task_id') not in (None, args.task):
                continue
            if skipped:
                trace['complete'] = False
            pairs.append((name, trace))
        if not pairs:
            write('没有找到执行记录（会话不存在、没有开启审计，或这个任务没有运行记录）。')
            return 1
        traces = [trace for _, trace in pairs]
        if args.task is None and not args.json:
            write(f'这个会话有 {len(pairs)} 次运行：')
            for name, trace in pairs:
                write(_summary(name, trace))
            write('用 --task 任务编号 查看某个任务的执行过程。')
            return 0
    if args.json:
        write(json.dumps(traces if len(traces) != 1 or not args.file else traces[0], ensure_ascii=False, indent=2))
        return 0
    for number, trace in enumerate(traces, 1):
        if len(traces) > 1:
            write(f'第 {number} 次运行')
        write(render_text(trace).rstrip('\n'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
