"""从目录建库：默认仅切块；确认后安全替换；可选清理过时来源（D-068、D-070）。"""

import argparse
import logging
import sys
from contextlib import contextmanager

from loguru import logger
from pathlib import Path

MAX_FAILURES_BEFORE_ANY_SUCCESS = 3
STAGE_LABELS = {
    'read': '读取或切块失败',
    'connect': '连接 Milvus 失败',
    'collection': 'Milvus 集合不可用',
    'embed_config': '没有配置嵌入密钥',
    'embed': '调用嵌入接口失败',
    'write': '写入 Milvus 失败',
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='python -m app.knowledge',
        description='把一个目录里的文档切块后写进知识库（Milvus）。默认只预演，不连接任何服务；'
                    '仅 --prune 预演会只读连接 Milvus 列出过时来源。')
    parser.add_argument('directory', help='要建库的目录，会递归查找支持的文件')
    parser.add_argument('--confirm', action='store_true',
                        help='真正写入：调用嵌入接口，先写新块再删除这些文件在 Milvus 里的旧块')
    parser.add_argument('--prune', action='store_true',
                        help='清理目录下不在本次文件列表里的来源（含排除目录和不支持的扩展名）；'
                             '预演会只读连接 Milvus，仅 --confirm 完整运行后才删除')
    parser.add_argument('--exclude', action='append', default=[], metavar='路径',
                        help='按相对路径排除文件或目录，可重复；--prune 会清理排除文件的已有来源')
    return parser


def _exception_type(exc: BaseException) -> str:
    seen = set()
    while exc.__cause__ is not None and id(exc) not in seen:
        seen.add(id(exc))
        exc = exc.__cause__
    return type(exc).__name__


def _failure(exc: BaseException) -> str:
    label = STAGE_LABELS.get(getattr(exc, 'stage', None))
    return f'{label}，{_exception_type(exc)}' if label else _exception_type(exc)


def _stop_hint(stages: list[str | None]) -> str:
    kinds = set(stages)
    if kinds == {'connect'}:
        return '请先启动 Milvus，例如 docker compose -f deploy/milvus/compose.yaml start。'
    if kinds == {'collection'}:
        return ('Milvus 在运行，但知识库集合不能用（常见原因是向量维度和 EMBEDDING_DIMENSIONS 不一致）；'
                '请核对配置，确需重建时见 MILVUS_ALLOW_DROP_ON_DIMENSION_MISMATCH 的说明。')
    if kinds == {'embed_config'}:
        return '请在 .env 里配置 DASHSCOPE_API_KEY。'
    if kinds == {'embed'}:
        return '嵌入接口调用失败，请检查密钥、网络和额度。'
    return '请先检查 Milvus 是否在运行、DASHSCOPE_API_KEY 是否已配置。'


def _prune(service, root: Path, files: list[Path], confirm: bool, out) -> tuple[int, int, int]:
    """返回已清理来源数、块数、失败数；预演只列来源。"""
    out('清理检查：只读连接 Milvus 列出来源，不调用嵌入接口。')
    store = service._store_manager
    try:
        sources = store.list_sources()
    except Exception as exc:
        from app.core.milvus_client import MilvusCollectionError, MilvusConnectError
        reason = ('连接 Milvus 失败' if isinstance(exc, MilvusConnectError)
                  else 'Milvus 集合不可用' if isinstance(exc, MilvusCollectionError) else '列出来源失败')
        out(f'清理检查失败：{reason}（{_exception_type(exc)}）')
        return 0, 0, 1
    current = {path.resolve().as_posix() for path in files}
    prefix = root.as_posix() + '/'
    stale = sorted(source for source in sources if source.startswith(prefix) and source not in current)
    if not confirm:
        for source in stale:
            out(f'  将清理：{source[len(prefix):]}')
        out(f'清理预演：{len(stale)} 个过时来源，没有删除任何块。')
        return 0, 0, 0
    removed = chunks = failed = 0
    for source in stale:
        try:
            count = store.delete_by_source(source)
        except Exception as exc:
            failed += 1
            out(f'  清理失败：{source[len(prefix):]}（写入 Milvus 失败，{_exception_type(exc)}）')
            continue
        removed += 1
        chunks += count
        out(f'  已清理：{source[len(prefix):]}，{count} 块')
    out(f'清理：{removed} 个已删除文件，共 {chunks} 块，失败 {failed} 个。')
    return removed, chunks, failed


@contextmanager
def _quiet_dependency_logs():
    """命令行入口专用：依赖可能把文档原文写进日志（D-033），只显示本命令自己的输出。

    只用公开接口。它只包住 main()：命令跑完进程就结束，所以退出时把标准日志开关
    恢复原值、把 Loguru 恢复为全部启用即可，不需要读写 Loguru 的内部状态。
    """
    previous_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    logger.disable('')
    logger.disable(None)
    try:
        yield
    finally:
        logger.enable(None)
        logger.enable('')
        logging.disable(previous_disable)


def run(args, service=None, out=print) -> int:
    """退出码：0 全部完成；1 有失败或提前停止；2 目录或排除参数无效。"""
    if service is None:
        from app.services.vector_index_service import vector_index_service as service
    from app.config import config
    directory = Path(args.directory)
    if not directory.is_dir():
        out(f'目录不存在：{directory}')
        return 2
    exclude = getattr(args, 'exclude', ())
    try:
        files = (service.supported_files(directory, exclude=exclude) if exclude
                 else service.supported_files(directory))
    except ValueError:
        out('排除路径无效：请使用非空且不含 .. 的相对路径。')
        return 2
    if exclude:
        excluded_count = len(service.supported_files(directory)) - len(files)
        out(f'按相对路径排除了 {excluded_count} 个文件。')
    kinds = '、'.join(config.rag_supported_extensions)
    prune = getattr(args, 'prune', False)
    if not files:
        out(f'没有找到可以建库的文件（支持 {kinds}）。')
        if not prune:
            return 0
    root = directory.resolve()

    def name(path):
        return path.relative_to(root).as_posix()

    if not args.confirm:
        total = failed = 0
        try:
            for path in files:
                try:
                    count = service.count_chunks(path)
                except Exception as exc:
                    failed += 1
                    out(f'  无法切块：{name(path)}（{_failure(exc)}）')
                    continue
                total += count
                out(f'  {name(path)}：{count} 块')
            out(f'预演：{len(files)} 个文件，共 {total} 块，{failed} 个无法切块。'
                '没有调用嵌入接口，也没有写入 Milvus；加 --confirm 才会真正写入。')
            if prune:
                failed += _prune(service, root, files, False, out)[2]
        finally:
            if prune:
                service.close()
        return 1 if failed else 0

    done = failed = chunks = 0
    stopped = False
    stages = []
    try:
        for path in files:
            try:
                count = service.index_single_file(path)
            except Exception as exc:
                failed += 1
                stages.append(getattr(exc, 'stage', None))
                out(f'  失败：{name(path)}（{_failure(exc)}）')
                if done == 0 and failed >= MAX_FAILURES_BEFORE_ANY_SUCCESS:
                    stopped = True
                    break
                continue
            done += 1
            chunks += count
            out(f'  已写入：{name(path)}，{count} 块')
        if prune:
            if stopped:
                out('建库提前停止，已跳过清理；没有删除过时来源。')
            else:
                failed += _prune(service, root, files, True, out)[2]
    finally:
        service.close()
    out(f'建库：{len(files)} 个文件，成功 {done} 个（共 {chunks} 块），失败 {failed} 个。')
    if stopped:
        out(f'前 {failed} 个文件都失败了，已停止，剩下 {len(files) - failed} 个没有处理。'
            + _stop_hint(stages))
    return 0 if failed == 0 else 1


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    with _quiet_dependency_logs():
        return run(args)


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
