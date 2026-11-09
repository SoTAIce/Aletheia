"""Directory-to-vector-store indexing orchestration."""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from loguru import logger

from app.config import config
from app.core.milvus_client import MilvusCollectionError, MilvusConnectError
from app.services.vector_embedding_service import (
    EmbeddingConfigError, EmbeddingRequestError, EmbeddingResponseError,
)
from app.services.document_splitter_service import document_splitter_service
from app.services.vector_store_manager import vector_store_manager


class IndexStageError(RuntimeError):
    """保留原异常链和消息，并标明建库失败阶段（D-070）。"""

    def __init__(self, stage: str, message: str) -> None:
        super().__init__(message)
        self.stage = stage


def _write_failure_stage(exc: BaseException) -> str:
    pending = [exc]
    seen: set[int] = set()
    chain: list[BaseException] = []
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        chain.append(current)
        pending.extend(e for e in (current.__cause__, current.__context__) if e is not None)
    if any(isinstance(e, MilvusConnectError) for e in chain):
        return "connect"
    if any(isinstance(e, MilvusCollectionError) for e in chain):
        return "collection"
    if any(isinstance(e, EmbeddingConfigError) for e in chain):
        return "embed_config"
    if any(isinstance(e, (EmbeddingRequestError, EmbeddingResponseError)) for e in chain):
        return "embed"
    return "write"


@dataclass
class IndexingResult:
    success: bool = False
    directory_path: str = ""
    total_files: int = 0
    success_count: int = 0
    fail_count: int = 0
    start_time: datetime | None = None
    end_time: datetime | None = None
    error_message: str = ""
    failed_files: dict[str, str] = field(default_factory=dict)

    def increment_success_count(self) -> None:
        self.success_count += 1

    def increment_fail_count(self) -> None:
        self.fail_count += 1

    def add_failed_file(self, file_path: str, error: str) -> None:
        self.failed_files[file_path] = error

    def get_duration_ms(self) -> int:
        if self.start_time and self.end_time:
            return int((self.end_time - self.start_time).total_seconds() * 1000)
        return 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "directory_path": self.directory_path,
            "total_files": self.total_files,
            "success_count": self.success_count,
            "fail_count": self.fail_count,
            "duration_ms": self.get_duration_ms(),
            "error_message": self.error_message,
            "failed_files": self.failed_files.copy(),
        }


# Directories that are never knowledge: version control, environments, caches (D-068).
EXCLUDED_DIRS = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".idea", ".vscode",
})


class VectorIndexService:
    def __init__(
        self,
        splitter: Any = document_splitter_service,
        store_manager: Any = vector_store_manager,
        upload_path: str = "./uploads",
    ) -> None:
        self.upload_path = upload_path
        self._splitter = splitter
        self._store_manager = store_manager

    def index_directory(self, directory_path: str | None = None) -> IndexingResult:
        result = IndexingResult(start_time=datetime.now())
        try:
            directory = Path(directory_path or self.upload_path).resolve()
            if not directory.is_dir():
                raise ValueError(f"directory does not exist or is not a directory: {directory}")

            result.directory_path = str(directory)
            files = self.supported_files(directory)
            result.total_files = len(files)

            for path in files:
                try:
                    self.index_single_file(path)
                    result.increment_success_count()
                except Exception as exc:
                    result.increment_fail_count()
                    result.add_failed_file(str(path), str(exc))
                    logger.exception("Failed to index file: {}", path)

            result.success = result.fail_count == 0
            if result.fail_count:
                result.error_message = f"{result.fail_count} file(s) failed to index"
        except Exception as exc:
            result.error_message = str(exc)
            logger.exception("Directory indexing failed")
        finally:
            result.end_time = datetime.now()
        return result

    @staticmethod
    def supported_files(directory: str | Path, exclude=()) -> list[Path]:
        """按路径段前缀排除相对路径；保留既有目录过滤和排序。"""
        prefixes = []
        for value in exclude:
            normalized = value.replace("\\", "/")
            # 两端包裹的斜杠可去掉；盘符、UNC 和单端绝对路径不能转成相对路径。
            if (PureWindowsPath(normalized).drive
                    or (normalized.startswith("/") and not normalized.endswith("/"))
                    or ".." in normalized.split("/")):
                raise ValueError("排除路径无效：请使用不含 .. 的相对路径")
            parts = PurePosixPath(normalized.strip("/")).parts
            if not parts:
                raise ValueError("排除路径无效：路径不能为空")
            prefixes.append(parts)
        root = Path(directory).resolve()
        supported = {suffix.lower() for suffix in config.rag_supported_extensions}
        return sorted(
            path
            for path in root.rglob("*")
            if path.is_file()
            and path.suffix.lower() in supported
            and not any(part in EXCLUDED_DIRS for part in path.relative_to(root).parts[:-1])
            and not any(path.relative_to(root).parts[:len(prefix)] == prefix for prefix in prefixes)
        )

    def _split(self, file_path: str | Path) -> tuple[str, list]:
        path = Path(file_path).resolve()
        if not path.is_file():
            raise ValueError(f"file does not exist: {path}")
        if path.suffix.lower() not in {
            suffix.lower() for suffix in config.rag_supported_extensions
        }:
            raise ValueError(f"unsupported file extension: {path.suffix}")
        normalized_path = path.as_posix()
        content = path.read_text(encoding="utf-8-sig")
        return normalized_path, self._splitter.split_document(content, normalized_path)

    def count_chunks(self, file_path: str | Path) -> int:
        """Split only, for a dry run: no embedding call and no vector store access (D-068)."""
        return len(self._split(file_path)[1])

    def index_single_file(self, file_path: str | Path) -> int:
        path = Path(file_path).resolve()
        try:
            normalized_path, documents = self._split(path)
        except Exception as exc:
            raise IndexStageError("read", f"failed to index {path}: {exc}") from exc
        try:
            return self._store_manager.replace_source(normalized_path, documents)
        except Exception as exc:
            raise IndexStageError(_write_failure_stage(exc), f"failed to index {path}: {exc}") from exc

    def close(self) -> None:
        close = getattr(self._store_manager, "close", None)
        if callable(close):
            close()


vector_index_service = VectorIndexService()
