import codecs
import os
import stat
from pathlib import Path
import tempfile


class FileAccess:
    def __init__(
        self,
        root: Path,
        max_read_bytes: int = 64 * 1024,
        *,
        write_root: Path | None = None,
        max_write_bytes: int = 64 * 1024,
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

        if not resolved_path.is_file():
            raise ValueError("path must point to a regular file")

        return resolved_path

    def read_file(self, path: str) -> str:
        resolved_path = self.resolve_read_path(path)

        with resolved_path.open("rb") as file:
            data = file.read(self._max_read_bytes + 1)

        if len(data) > self._max_read_bytes:
            raise ValueError(
                f"file exceeds the read limit of {self._max_read_bytes} bytes"
            )

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
