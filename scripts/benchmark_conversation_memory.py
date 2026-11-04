"""Offline, fsync-preserving conversation I/O benchmark; timings are descriptive."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import importlib.util
from pathlib import Path
import statistics
import sys
import tempfile
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.models.conversation_memory import ConversationMemory, ConversationMessage, ConversationTurn, MessageRole
from app.services.conversation_archive import ConversationArchive

STAMP = datetime(2025, 1, 1, tzinfo=timezone.utc)


def turn(index):
    return ConversationTurn([ConversationMessage(MessageRole.USER, 'x' * 1024, STAMP)],
                            turn_id=f'bench-{index}', created_at=STAMP)


class NoModel:
    async def compress(self, previous_summary, turns):
        raise AssertionError('benchmark must not call a model')


async def workload(size, mode, base, repeats):
    root = base / f'{size}-{mode}'
    archive = ConversationArchive(root, 'benchmark')
    # Compatible immutable fixtures, built outside measured operations.
    for index in range(size):
        item = turn(index)
        path = archive._directory / f'{index:020d}-{archive._digest(item.turn_id)}.json'
        path.write_text(json.dumps(archive._record(item)), encoding='utf-8')
    if mode != 'cold_load':
        archive.load_turns(20)
    memory = None
    if mode.startswith('add'):
        memory = ConversationMemory('benchmark', archive=archive,
            compressor=NoModel() if mode == 'add_compressor' else None,
            compression_token_threshold=10**9)
        await memory.restore()
    samples = []
    for repetition in range(repeats):
        if mode == 'cold_load':
            archive = ConversationArchive(root, 'benchmark')
        counts = {'body_reads': 0, 'directory_audits': 0}
        read, paths = archive._read, archive._paths
        def counted_read(path):
            counts['body_reads'] += 1
            return read(path)
        def counted_paths():
            counts['directory_audits'] += 1
            return paths()
        archive._read, archive._paths = counted_read, counted_paths
        try:
            start = perf_counter()
            if mode == 'save':
                archive.save_turn(turn(size + repetition))
            elif mode.startswith('add'):
                await memory.add(turn(size + repetition))
            else:
                archive.load_turns(None if mode == 'full_load' else 20)
            elapsed = (perf_counter() - start) * 1000
        finally:
            archive._read, archive._paths = read, paths
        samples.append({'ms': round(elapsed, 3), **counts})
    return {'history': size, 'mode': mode, 'median_ms': statistics.median(s['ms'] for s in samples),
            'median_body_reads': statistics.median(s['body_reads'] for s in samples),
            'median_directory_audits': statistics.median(s['directory_audits'] for s in samples),
            'samples': samples}


async def run(args):
    base = Path(tempfile.mkdtemp(prefix='conversation-bench-', dir='tmp')).absolute()
    results = [await workload(size, mode, base, args.repeats)
               for size in args.sizes
               for mode in ('save', 'add', 'add_compressor', 'warm_load', 'cold_load', 'full_load')]
    output = {'label': args.label, 'python': sys.version, 'fixture_root': str(base),
              'repeats': args.repeats, 'text_chars': 1024, 'max_turns': 20,
              'note': 'JSON decoding and directory audit counts; metadata remains O(history). No APIs.',
              'results': results}
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(output, indent=2) + '\n', encoding='utf-8')
    for row in results:
        print(f"{row['history']:4} {row['mode']:16} {row['median_ms']:9.3f} ms "
              f"bodies={row['median_body_reads']} audits={row['median_directory_audits']}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--label', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--sizes', type=int, nargs='+', default=[100, 500])
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--source-snapshot', type=Path,
                        help='Replay the identical workload against trusted local source snapshots')
    args = parser.parse_args()
    if args.source_snapshot:
        # Only explicit, application-owned local snapshots; never download code.
        for name, relative in [('app.models.conversation_memory', 'app/models/conversation_memory.py'),
                               ('app.services.conversation_archive', 'app/services/conversation_archive.py')]:
            spec = importlib.util.spec_from_file_location(name, args.source_snapshot / relative)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
        memory_module = sys.modules['app.models.conversation_memory']
        ConversationMemory = memory_module.ConversationMemory
        ConversationTurn = memory_module.ConversationTurn
        ConversationMessage = memory_module.ConversationMessage
        MessageRole = memory_module.MessageRole
        ConversationArchive = sys.modules['app.services.conversation_archive'].ConversationArchive
    asyncio.run(run(args))
