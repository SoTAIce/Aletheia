"""Build the knowledge base from a directory: python -m app.knowledge DIRECTORY [--confirm] (D-068).

Without --confirm it is a dry run: files are listed and split, nothing is embedded and
nothing is written. With --confirm each file is split, its old chunks are deleted from
Milvus and the new ones are embedded and written, through VectorIndexService. Failures
are reported by file and exception type only, never with provider text (D-033).
Chunks of files that were removed from the directory are not deleted.
"""

import argparse
import sys
from pathlib import Path

MAX_FAILURES_BEFORE_ANY_SUCCESS = 3   # e.g. Milvus down or no embedding key: stop early


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='python -m app.knowledge',
        description='把一个目录里的文档切块后写进知识库（Milvus）。默认只预演，不连接任何服务。')
    parser.add_argument('directory', help='要建库的目录，会递归查找支持的文件')
    parser.add_argument('--confirm', action='store_true',
                        help='真正写入：调用嵌入接口，并替换这些文件在 Milvus 里原有的块')
    return parser


def _failure(exc: BaseException) -> str:
    return type(exc.__cause__ or exc).__name__


def run(args, service=None, out=print) -> int:
    """0: every file done; 1: some file failed or the run stopped early; 2: bad directory."""
    if service is None:
        from app.services.vector_index_service import vector_index_service as service
    from app.config import config
    directory = Path(args.directory)
    if not directory.is_dir():
        out(f'目录不存在：{directory}')
        return 2
    files = service.supported_files(directory)
    kinds = '、'.join(config.rag_supported_extensions)
    if not files:
        out(f'没有找到可以建库的文件（支持 {kinds}）。')
        return 0
    root = directory.resolve()

    def name(path):
        return path.relative_to(root).as_posix()

    if not args.confirm:
        total = failed = 0
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
        return 1 if failed else 0

    done = failed = chunks = 0
    stopped = False
    try:
        for path in files:
            try:
                count = service.index_single_file(path)
            except Exception as exc:
                failed += 1
                out(f'  失败：{name(path)}（{_failure(exc)}）')
                if done == 0 and failed >= MAX_FAILURES_BEFORE_ANY_SUCCESS:
                    stopped = True
                    break
                continue
            done += 1
            chunks += count
            out(f'  已写入：{name(path)}，{count} 块')
    finally:
        service.close()
    out(f'建库：{len(files)} 个文件，成功 {done} 个（共 {chunks} 块），失败 {failed} 个。')
    if stopped:
        out(f'前 {failed} 个文件都失败了，已停止，剩下 {len(files) - failed} 个没有处理。'
            '请先检查 Milvus 是否在运行、DASHSCOPE_API_KEY 是否已配置。')
    return 0 if failed == 0 else 1


def main(argv=None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
