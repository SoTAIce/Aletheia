import codecs
import os
import stat
from pathlib import Path
import tempfile
import re


# Host-fixed names the model may never read, list or write, whatever the root (D-029).
# Credentials and VCS internals: a read would send them to the model provider and the archive.
_SECRET_DIRS = frozenset({".git", ".ssh", ".aws", ".gnupg", ".azure", ".kube", ".docker"})
_SECRET_FILES = frozenset({".env", ".netrc", ".npmrc", ".pypirc", ".git-credentials",
                           "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "credentials.json"})
_SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore")
# Places the host itself runs later (hooks, CI, IDE tasks, interpreter start-up): written
# there, model code would run outside the sandbox. Reading them is harmless.
_AUTORUN_DIRS = frozenset({".github", ".vscode", ".idea", ".devcontainer"})
_AUTORUN_FILES = frozenset({"sitecustomize.py", "usercustomize.py"})
_AUTORUN_SUFFIXES = (".pth",)
PROTECTED_MESSAGE = "path is protected: secrets, VCS internals and auto-run files are off limits"
DENIED_MESSAGE = "path is in a folder the user has closed to the agent"


def _is_secret_name(name: str) -> bool:
    lower = name.lower()
    return (lower in _SECRET_FILES or lower.endswith(_SECRET_SUFFIXES)
            or (lower.startswith(".env.") and lower != ".env.example"))


def is_secret_path(parts) -> bool:
    """True when a root-relative path is or lies inside a secret file or directory."""
    parts = [p.lower() for p in parts]
    return any(p in _SECRET_DIRS for p in parts) or bool(parts and _is_secret_name(parts[-1]))


def is_protected_write_path(parts) -> bool:
    """Secrets plus the places the host runs automatically."""
    lowered = [p.lower() for p in parts]
    return (is_secret_path(parts) or any(p in _AUTORUN_DIRS for p in lowered)
            or bool(lowered and (lowered[-1] in _AUTORUN_FILES or lowered[-1].endswith(_AUTORUN_SUFFIXES))))


class FileAccess:
    def __init__(
        self,
        root: Path,
        max_read_bytes: int = 64 * 1024,
        *,
        write_root: Path | None = None,
        max_write_bytes: int = 64 * 1024,
        denied: tuple = (),
    ) -> None:
        if not isinstance(root, Path):
            raise TypeError("root must be a pathlib.Path")

        if not root.is_absolute():
            raise ValueError("root must be absolute Path")

        if type(max_read_bytes) is not int:
            raise TypeError("max_read_bytes must be integer")

        if max_read_bytes <= 0:
            raise ValueError("max_read_bytes must be positive")

        resolved_root = root.resolve(strict=True)

        if not resolved_root.is_dir():
            raise NotADirectoryError("root must be a directory")

        if type(max_write_bytes) is not int:
            raise TypeError("max_write_bytes must be an integer")
        if max_write_bytes <= 0:
            raise ValueError("max_write_bytes must be positive")

        resolved_write_root = None
        if write_root is not None:
            if not isinstance(write_root, Path):
                raise TypeError("write_root must be a pathlib.Path")
            if not write_root.is_absolute():
                raise ValueError("write_root must be absolute")
            resolved_write_root = write_root.resolve(strict=True)
            if not resolved_write_root.is_dir():
                raise NotADirectoryError("write_root must be a directory")

        self._write_root = resolved_write_root
        self._max_write_bytes = max_write_bytes
        self._root = resolved_root
        self._max_read_bytes = max_read_bytes
        self._max_list_entries: int = 200
        # Folders the user closed to the agent (workspace-001): never read, listed, searched or
        # written, as if they did not exist; compared after resolving links.
        self._denied = tuple(Path(d).resolve() for d in denied)

    def _is_denied(self, path: Path) -> bool:
        return any(path == d or d in path.parents for d in self._denied)

    def resolve_read_path(self, path: str) -> Path:
        if not isinstance(path, str):
            raise TypeError("path must be a string")

        if not path.strip():
            raise ValueError("path must not be empty")

        if "\x00" in path:
            raise ValueError("path must not contain null bytes")

        relative_path = Path(path)

        if relative_path.drive or relative_path.root:
            raise ValueError("path must be relative")

        if ".." in relative_path.parts:
            raise ValueError("parent directory traversal is not allowed")

        if ":" in path:
            raise ValueError("alternate data streams are not allowed")

        candidate = self._root / relative_path

        resolved_path = candidate.resolve(strict=True)

        if not resolved_path.is_relative_to(self._root):
            raise PermissionError("path is outside the allowed root")

        if is_secret_path(resolved_path.relative_to(self._root).parts):
            raise PermissionError(PROTECTED_MESSAGE)

        if self._is_denied(resolved_path):
            raise PermissionError(DENIED_MESSAGE)

        if not resolved_path.is_file():
            raise ValueError("path must point to a regular file")

        return resolved_path

    def read_file(self, path: str, start_line: int | None = None, max_lines: int | None = None) -> str:
        """Whole file, or a 1-based line range when start_line or max_lines is given (D-052).

        A ranged read ends with a note naming the lines shown, the total, and where to
        continue, so a long source file can be read piece by piece.
        """
        for name, value in (("start_line", start_line), ("max_lines", max_lines)):
            if value is not None and (type(value) is not int or value < 1):
                raise ValueError(f"{name} must be a positive integer")
        text = self._read_whole(path)
        if start_line is None and max_lines is None:
            return text
        lines = text.splitlines(keepends=True)
        first = start_line or 1
        if first > max(len(lines), 1):
            raise ValueError(f"start_line {first} is past the end of the file ({len(lines)} lines)")
        chosen = lines[first - 1:first - 1 + max_lines] if max_lines is not None else lines[first - 1:]
        last = first + len(chosen) - 1
        body = "".join(chosen)
        if body and not body.endswith("\n"):
            body += "\n"
        note = f"[lines {first}-{last} of {len(lines)}"
        note += f"; continue with start_line={last + 1}]" if last < len(lines) else "; end of file]"
        return body + note

    def _read_whole(self, path: str) -> str:
        resolved_path = self.resolve_read_path(path)

        with resolved_path.open("rb") as file:
            data = file.read(self._max_read_bytes + 1)

        if len(data) > self._max_read_bytes:
            raise ValueError(
                f"file exceeds the read limit of {self._max_read_bytes} bytes"
            )
        return self._decode(data)

    # ---- search (D-055) ---------------------------------------------------------------
    # Directories never worth searching: caches, installed packages, virtual environments.
    _SEARCH_SKIP_DIRS = frozenset({"__pycache__", "node_modules", "site-packages", "venv", ".venv"})
    SEARCH_MAX_FILES = 5000
    SEARCH_MAX_RESULTS = 200
    SEARCH_LINE_CHARS = 300

    def search_files(self, pattern: str, path: str = ".", glob: str | None = None,
                     regex: bool = False, ignore_case: bool = False, max_results: int = 50) -> dict:
        """Lines matching pattern under path, as {path, line, text}, for reading around them
        with read_file(start_line=...). Read-only and bounded: hidden, secret and cache
        directories, symlinks, binary files and files over the read limit are skipped."""
        import fnmatch
        if not isinstance(pattern, str) or not pattern or len(pattern) > 200:
            raise ValueError("pattern must be a non-empty string of at most 200 characters")
        if glob is not None and (not isinstance(glob, str) or not glob or len(glob) > 100):
            raise ValueError("glob must be a non-empty string of at most 100 characters")
        for name, value in (("regex", regex), ("ignore_case", ignore_case)):
            if type(value) is not bool:
                raise ValueError(f"{name} must be true or false")
        if type(max_results) is not int or not 1 <= max_results <= self.SEARCH_MAX_RESULTS:
            raise ValueError(f"max_results must be between 1 and {self.SEARCH_MAX_RESULTS}")
        flags = re.IGNORECASE if ignore_case else 0
        try:
            matcher = re.compile(pattern if regex else re.escape(pattern), flags)
        except re.error as exc:
            raise ValueError(f"invalid regular expression: {exc.msg}") from None

        start = self.resolve_read_path(path) if (self._root / path).is_file() else self.resolve_directory_path(path)
        files = [start] if start.is_file() else self._search_walk(start)
        matches, searched, skipped, truncated = [], 0, 0, False
        for file in files:
            relative = file.relative_to(self._root).as_posix()
            if glob is not None and not (fnmatch.fnmatch(file.name, glob) or fnmatch.fnmatch(relative, glob)):
                continue
            if searched >= self.SEARCH_MAX_FILES:
                truncated = True
                break
            searched += 1
            try:
                with file.open("rb") as handle:
                    data = handle.read(self._max_read_bytes + 1)
                if len(data) > self._max_read_bytes or b"\x00" in data:
                    skipped += 1
                    continue
                text = self._decode(data)
            except (OSError, ValueError):
                skipped += 1
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if matcher.search(line[:2000]):
                    if len(matches) >= max_results:
                        truncated = True
                        break
                    matches.append({"path": relative, "line": number,
                                    "text": line.strip()[:self.SEARCH_LINE_CHARS]})
            if truncated:
                break
        return {"matches": matches, "files_searched": searched, "files_skipped": skipped,
                "truncated": truncated}

    def _search_walk(self, directory: Path):
        for current, dirs, names in os.walk(directory, followlinks=False):
            base = Path(current)
            dirs[:] = sorted(d for d in dirs
                             if not d.startswith(".") and d not in self._SEARCH_SKIP_DIRS
                             and not (base / d).is_symlink()
                             and not is_secret_path((base / d).relative_to(self._root).parts)
                             and not self._is_denied(base / d))
            for name in sorted(names):
                file = base / name
                if (file.is_symlink() or is_secret_path(file.relative_to(self._root).parts)
                        or self._is_denied(file)):
                    continue
                yield file

    @staticmethod
    def _decode(data: bytes) -> str:
        # A BOM is authoritative; malformed marked data must not fall back.
        # UTF-32 LE shares its first two BOM bytes with UTF-16 LE.
        if data.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
            encodings = ("utf-32",)
        elif data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
            encodings = ("utf-16",)
        elif data.startswith(codecs.BOM_UTF8):
            encodings = ("utf-8-sig",)
        else:
            encodings = ("utf-8", "gb18030")

        last_error = None
        for encoding in encodings:
            try:
                return data.decode(encoding, errors="strict")
            except UnicodeError as exc:
                last_error = exc
        raise ValueError(
            f"cannot decode file using {', '.join(encodings)}; "
            "the file may be damaged or use an unsupported encoding"
        ) from last_error

    def resolve_directory_path(self, path: str) -> Path:
        if not isinstance(path, str):
            raise TypeError("path must be a string")

        if not path.strip():
            raise ValueError("path must not be empty")

        if "\x00" in path:
            raise ValueError("path must not contain null bytes")

        relative_path = Path(path)

        candidate = self._root / relative_path
        
        resolved_path = candidate.resolve(strict=True)

        if not resolved_path.is_relative_to(self._root):
            raise PermissionError("path is outside the allowed root")

        if is_secret_path(resolved_path.relative_to(self._root).parts):
            raise PermissionError(PROTECTED_MESSAGE)

        if self._is_denied(resolved_path):
            raise PermissionError(DENIED_MESSAGE)

        if not resolved_path.is_dir():
            raise ValueError("path must point to a directory")

        return resolved_path


    def list_directory(
                self,
                path: str = ".",
                offset: int = 0,
                limit: int = 50,
        ) -> dict:
        resolved_path = self.resolve_directory_path(path)
        entries = []
        for child in resolved_path.iterdir():
            if is_secret_path((child.name,)) or self._is_denied(child):
                continue            # never reveal that a secret or a closed folder exists (D-029)
            if child.is_file() and not child.is_symlink():
                entries.append({
                    "name": child.name,
                    "type": "file",
                })
            elif child.is_dir() and not child.is_symlink():
                entries.append({
                    "name": child.name,
                    "type": "directory",
                })
        entries.sort(
            key = lambda item: (
                item["type"] != "directory",
                item["name"].lower(),
            )
        )

        total = len(entries)
        page = entries[offset: offset + limit]

        has_more = offset + limit < total

        return {
            "entries": page,
            "offset": offset,
            "limit": limit,
            "total": total,
            "has_more": has_more,
        }


    def edit_file(self, path: str, old: str, new: str, replace_all: bool = False) -> dict:
        """Replace old with new in one existing file under the WRITE root (D-087).

        old must appear exactly once unless replace_all is true. Only UTF-8 files are edited
        (a BOM is kept); a file with CRLF line ends matches an old written with LF. The file
        and the result stay within the write limit, and the result is written atomically.
        Returns the number of replacements and the line where the first one starts.
        """
        for name, value in (("old", old), ("new", new)):
            if not isinstance(value, str):
                raise TypeError(f"{name} must be a string")
        if not isinstance(replace_all, bool):
            raise TypeError("replace_all must be a bool")
        if not old:
            raise ValueError("old must not be empty")
        if old == new:
            raise ValueError("old and new are the same; nothing to change")
        resolved_path = self.resolve_write_path(path)
        if not resolved_path.is_file():
            raise FileNotFoundError("edit_file changes an existing file; create new files with write_file")
        with resolved_path.open("rb") as file:
            data = file.read(self._max_write_bytes + 1)
        if len(data) > self._max_write_bytes:
            raise ValueError(f"file exceeds the write limit of {self._max_write_bytes} bytes")
        bom = data.startswith(codecs.BOM_UTF8)
        try:
            text = data[len(codecs.BOM_UTF8):].decode("utf-8") if bom else data.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("edit_file only edits UTF-8 files; rewrite this file with write_file") from None
        count = text.count(old)
        if count == 0 and "\r\n" in text and "\n" in old and "\r" not in old:
            # The model wrote LF; the file uses CRLF.
            old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
            count = text.count(old)
        if count == 0:
            raise ValueError("old text not found; read the lines again with read_file and copy them exactly")
        if count > 1 and not replace_all:
            raise ValueError(f"old text appears {count} times; include more surrounding lines "
                             "or set replace_all=true")
        line = text.count("\n", 0, text.index(old)) + 1
        updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        result = (codecs.BOM_UTF8 if bom else b"") + updated.encode("utf-8")
        if len(result) > self._max_write_bytes:
            raise ValueError(f"the edited file would exceed the write limit of {self._max_write_bytes} bytes")
        self._write_atomic(resolved_path, result)
        return {"replacements": count if replace_all else 1, "line": line}

    def resolve_write_path(self, path: str) -> Path:
        """Validate a destination against the configured write permissions.

        The target may be new; its parent must exist and be an allowed directory.
        Validate path syntax and links before returning an absolute destination.
        Do not create directories or modify files here.
        """
        if self._write_root is None:
            raise PermissionError("file writing is disabled; configure write_root")
        if not isinstance(path, str):
            raise TypeError("path must be a string")
        if not path.strip() or "\x00" in path:
            raise ValueError("path must be non-empty and contain no null bytes")

        relative_path = Path(path)
        if relative_path.drive or relative_path.root:
            raise ValueError("path must be relative to write_root")
        if not relative_path.parts or ".." in relative_path.parts:
            raise ValueError("path must name a file without parent traversal")
        if ":" in path:
            raise ValueError("alternate data streams are not allowed")
        if path.endswith(("/", "\\")):
            raise ValueError("path must name a file, not a directory")
        if is_protected_write_path(relative_path.parts):
            raise PermissionError(PROTECTED_MESSAGE)
        if self._is_denied(self._write_root / relative_path):
            raise PermissionError(DENIED_MESSAGE)

        # Reject Windows device names and ambiguous filename normalization.
        reserved = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
        reserved.update(f"{prefix}{suffix}" for prefix in ("COM", "LPT")
                        for suffix in "123456789\u00b9\u00b2\u00b3")
        for part in relative_path.parts:
            if (part.endswith((".", " "))
                    or any(ord(char) < 32 or char in '<>"|?*' for char in part)
                    or part.split(".")[0].rstrip(" ").upper() in reserved):
                raise ValueError("path contains an unsupported filename")

        candidate = self._write_root
        for index, part in enumerate(relative_path.parts):
            candidate = candidate / part
            try:
                info = candidate.lstat()
            except FileNotFoundError:
                if index != len(relative_path.parts) - 1:
                    raise FileNotFoundError("destination parent does not exist") from None
                break
            if (stat.S_ISLNK(info.st_mode)
                    or getattr(info, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
                raise PermissionError("links and reparse points are not allowed for writing")
            if index < len(relative_path.parts) - 1:
                if not stat.S_ISDIR(info.st_mode):
                    raise NotADirectoryError("destination parent must be a directory")
            elif not stat.S_ISREG(info.st_mode):
                raise ValueError("destination must be a regular file")

        parent = candidate.parent.resolve(strict=True)
        if not parent.is_relative_to(self._write_root):
            raise PermissionError("path is outside the allowed write root")
        destination = parent / candidate.name
        if not destination.resolve(strict=False).is_relative_to(self._write_root):
            raise PermissionError("path is outside the allowed write root")
        return destination

        
    def write_file(
        self,
        path: str,
        content: str,
        *,
        overwrite: bool = False,
    ) -> None:

        if not isinstance(content, str):
            raise TypeError("content must be a string")
        if not isinstance(overwrite, bool):
            raise TypeError("overwrite must be a bool")

        data = content.encode("utf-8")

        if len(data) > self._max_write_bytes:
            raise ValueError(
                f"content exceeds the write limit of "
                f"{self._max_write_bytes} bytes"
            )

        resolved_path = self.resolve_write_path(path)

        if not overwrite:
            with resolved_path.open("xb") as file:
                file.write(data)
            return

        self._write_atomic(resolved_path, data)

    @staticmethod
    def _write_atomic(resolved_path: Path, data: bytes) -> None:
        """Write to a temporary file beside the destination, then replace it (write_file, edit_file)."""
        temporary_path: Path | None = None

        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=resolved_path.parent,
                prefix=".aletheia-",
                suffix=".tmp",
                delete=False,
            ) as file:
                temporary_path = Path(file.name)
                file.write(data)
                file.flush()
                os.fsync(file.fileno())

            temporary_path.replace(resolved_path)

        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


