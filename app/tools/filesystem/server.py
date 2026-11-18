"""Start from the project root with python -m app.tools.filesystem.server."""

import argparse
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from app.tools.filesystem.file_access import FileAccess

# What each tool does to the workspace, for the host (D-087).
READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)


def create_server(
    root: Path,
    *,
    write_root: Path | None = None,
    max_read_bytes: int = 64 * 1024,
    max_write_bytes: int = 64 * 1024,
    denied: tuple = (),
) -> FastMCP:
    file_access = FileAccess(
        root=root,
        write_root=write_root,
        max_read_bytes=max_read_bytes,
        max_write_bytes=max_write_bytes,
        denied=tuple(denied),
    )
    mcp = FastMCP("AletheiaFilesystem")

    @mcp.tool(annotations=READ)
    def read_file(path: str, start_line: int | None = None, max_lines: int | None = None) -> str:
        """Read a text file relative to the configured READ root.

        Absolute paths and parent traversal are forbidden. Reads are bounded
        by the configured byte limit. Unicode BOMs are recognized; otherwise
        UTF-8 is tried before GB18030. The file must already exist.
        Optional start_line (1-based) and max_lines read a range of lines; the
        result then ends with a note like "[lines 1-200 of 540; continue with
        start_line=201]". Use them for long files instead of reading saved copies.
        To find where something is defined or used, call search_files first and
        read only the lines around its matches.
        """
        return file_access.read_file(path, start_line=start_line, max_lines=max_lines)

    @mcp.tool(annotations=READ)
    def search_files(pattern: str, path: str = ".", glob: str | None = None, regex: bool = False,
                     ignore_case: bool = False, max_results: int = 50) -> dict:
        """Find lines containing pattern in text files under path (a directory or one file)
        relative to the READ root. Returns matches as {path, line, text}, plus how many files
        were searched or skipped and whether results were cut off.

        pattern is plain text unless regex=true. glob filters file names or paths, e.g.
        "*.py" or "app/agent/*.py". max_results is 1-200 (default 50). Hidden, secret and
        cache directories, binary files and files over the read limit are skipped.
        Use it to locate a function or name, then read_file(path, start_line=line-20,
        max_lines=80) around the match instead of reading whole files.
        """
        return file_access.search_files(pattern, path=path, glob=glob, regex=regex,
                                        ignore_case=ignore_case, max_results=max_results)

    if write_root is not None:
        read_root = root.resolve(strict=True)
        output_root = write_root.resolve(strict=True)

        def readable(path):
            destination = output_root / path
            if destination.is_relative_to(read_root):
                return destination.relative_to(read_root).as_posix()
            return None

        @mcp.tool(annotations=WRITE)
        def write_file(path: str, content: str, overwrite: bool = False) -> str:
            """Write UTF-8 text relative to the configured WRITE root.

            Use it for new files and for rewriting a whole file; to change part of an
            existing file, use edit_file. The WRITE root may differ from the READ root.
            Parents must exist. Existing files are protected unless overwrite=true. Empty
            content is allowed. Links, traversal and oversized content are rejected.
            This replaces content; it never appends. On success, a read_file
            path is returned when the destination is also within the READ root.
            """
            file_access.write_file(path=path, content=content, overwrite=overwrite)
            read_path = readable(path)
            if read_path is not None:
                return f"File written successfully. Read it with read_file(path={read_path!r})."
            return "File written successfully. The destination is outside the configured READ root."

        @mcp.tool(annotations=WRITE)
        def edit_file(path: str, old: str, new: str, replace_all: bool = False) -> str:
            """Change part of an existing UTF-8 file under the WRITE root by replacing text (D-087).

            old is the exact text to replace, copied from read_file (without its line note);
            it must appear exactly once, so include enough surrounding lines to make it
            unique, or set replace_all=true to replace every occurrence. new replaces it
            (it may be empty to delete). Prefer this to rewriting a whole file with
            write_file. Line ends may be written as \\n in a file that uses \\r\\n.
            """
            done = file_access.edit_file(path=path, old=old, new=new, replace_all=replace_all)
            text = (f"Edited {path}: replaced {done['replacements']} occurrence(s) starting at "
                    f"line {done['line']}.")
            read_path = readable(path)
            if read_path is not None:
                text += (f" Read it with read_file(path={read_path!r}, "
                         f"start_line={max(1, done['line'] - 5)}, max_lines=40).")
            return text

    @mcp.tool(annotations=READ)
    def list_directory(path: str = ".", offset: int = 0, limit: int = 50) -> dict:
        """List files and directories under the configured READ root."""
        return file_access.list_directory(path=path, offset=offset, limit=limit)

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(description="Aletheia stdio filesystem MCP server")
    parser.add_argument("--root", type=Path, required=True, help="Existing absolute READ directory")
    parser.add_argument("--write-root", type=Path, help="Existing absolute WRITE directory; omitted means read-only")
    parser.add_argument("--max-read-bytes", type=int, default=64 * 1024)
    parser.add_argument("--max-write-bytes", type=int, default=64 * 1024)
    parser.add_argument("--deny", type=Path, action="append", default=[],
                        help="A folder the agent may never read or write (repeatable)")
    args = parser.parse_args()
    try:
        server = create_server(
            args.root,
            write_root=args.write_root,
            max_read_bytes=args.max_read_bytes,
            max_write_bytes=args.max_write_bytes,
            denied=tuple(args.deny),
        )
    except (OSError, ValueError, TypeError) as exc:
        parser.error(str(exc))
    server.run(transport="stdio")


if __name__ == "__main__":
    main()



