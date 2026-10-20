"""Immutable, session-bound conversation originals for a single process."""
from __future__ import annotations

import hashlib
from collections import deque
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
from datetime import UTC, datetime
from uuid import uuid4

from app.models.conversation_memory import ConversationMessage, ConversationTurn, MessageRole, SummaryEntry, ConversationMemory


class ConversationArchive:
    """Publish one fsynced JSON file per turn; never overwrite an original.

    All instances in this process serialize publication for the same session.
    The directory must be owned by the application (not concurrently modified
    by an untrusted process). Symlinks and Windows junctions fail closed.
    """

    _locks: dict[Path, threading.RLock] = {}
    _locks_guard = threading.Lock()

    def __init__(self, root: str | Path, session_id: str) -> None:
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("invalid archive session_id")
        self._root = Path(os.path.abspath(root))
        self._session_id = session_id
        session_digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        self._session_digest = session_digest
        self._directory = self._filesystem_path(self._root) / f"session-{session_digest}" / "turns"
        self._check_path(self._directory)
        self._directory.mkdir(parents=True, exist_ok=True)
        self._check_path(self._directory)
        with self._locks_guard:
            self._lock = self._locks.setdefault(self._directory, threading.RLock())

    @property
    def root(self) -> Path:
        return self._root

    @property
    def session_id(self) -> str:
        return self._session_id

    @staticmethod
    def _filesystem_path(path: Path) -> Path:
        """Use extended Windows paths internally, preserving the public root."""
        value = str(path)
        if os.name != "nt" or value.startswith("\\\\?\\"):
            return path
        if value.startswith("\\\\"):
            return Path("\\\\?\\UNC\\" + value[2:])
        return Path("\\\\?\\" + value)

    @staticmethod
    def _check_path(path: Path) -> None:
        for component in (path, *path.parents):
            if component.is_symlink() or (hasattr(component, "is_junction") and component.is_junction()):
                raise ValueError("archive path must not contain symlinks or junctions")
            if os.name == "nt":
                try:
                    attributes = getattr(component.lstat(), "st_file_attributes", 0)
                except FileNotFoundError:
                    continue
                if attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    raise ValueError("archive path must not contain Windows reparse points")

    @staticmethod
    def _digest(turn_id: str) -> str:
        if not isinstance(turn_id, str) or not turn_id:
            raise ValueError("archive turn_id must be a nonempty string")
        return hashlib.sha256(turn_id.encode("utf-8")).hexdigest()

    def _record(self, turn: ConversationTurn) -> dict:
        if not isinstance(turn, ConversationTurn):
            raise TypeError("turn must be a ConversationTurn")
        self._digest(turn.turn_id)
        if not turn.messages or not isinstance(turn.created_at, datetime):
            raise ValueError("invalid archive turn")
        messages = []
        for message in turn.messages:
            if not isinstance(message.role, MessageRole) or not isinstance(message.content, str) or not isinstance(message.created_at, datetime):
                raise ValueError("invalid archive message")
            messages.append({"role": message.role.value, "content": message.content, "created_at": message.created_at.isoformat()})
        return {"version": 1, "session_id": self.session_id, "turn_id": turn.turn_id,
                "created_at": turn.created_at.isoformat(), "messages": messages}

    def _read(self, path: Path) -> ConversationTurn:
        self._check_path(path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if set(data) != {"version", "session_id", "turn_id", "created_at", "messages"} or data["version"] != 1 or data["session_id"] != self.session_id:
                raise ValueError("invalid record metadata")
            if path.name[21:] != f"{self._digest(data['turn_id'])}.json" or not path.name[:20].isdigit():
                raise ValueError("invalid record identity")
            messages = []
            for item in data["messages"]:
                if set(item) != {"role", "content", "created_at"} or not isinstance(item["content"], str):
                    raise ValueError("invalid message")
                messages.append(ConversationMessage(MessageRole(item["role"]), item["content"], datetime.fromisoformat(item["created_at"])))
            turn = ConversationTurn(messages, data["turn_id"], datetime.fromisoformat(data["created_at"]))
            self._record(turn)
            return turn
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise ValueError(f"malformed archive record: {path.name}") from exc

    def _paths(self) -> list[Path]:
        self._check_path(self._directory)
        paths = sorted(self._directory.glob("*.json"))
        sequences = set()
        for path in paths:
            if not re.fullmatch(r"[0-9]{20}-[0-9a-f]{64}\.json", path.name):
                raise ValueError(f"malformed archive filename: {path.name}")
            sequence = path.name[:20]
            if sequence in sequences:
                raise ValueError("malformed archive: duplicate turn sequence")
            sequences.add(sequence)
        return paths

    def save_turn(self, turn: ConversationTurn) -> None:
        record = self._record(turn)
        with self._lock:
            paths = self._paths()
            # Validate existing records, rather than silently extending corrupt history.
            for path in paths:
                existing = self._read(path)
                if existing.turn_id == turn.turn_id:
                    if self._record(existing) != record:
                        raise ValueError(f"archive turn ID conflict: {turn.turn_id}")
                    return
            sequence = int(paths[-1].name[:20]) + 1 if paths else 0
            destination = self._directory / f"{sequence:020d}-{self._digest(turn.turn_id)}.json"
            fd, temporary = tempfile.mkstemp(prefix=".turn-", suffix=".tmp", dir=self._directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                    json.dump(record, handle, ensure_ascii=False, sort_keys=True)
                    handle.flush()
                    os.fsync(handle.fileno())
                self._check_path(self._directory)
                os.replace(temporary, destination)
                if os.name != "nt":
                    directory_fd = os.open(self._directory, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
            finally:
                Path(temporary).unlink(missing_ok=True)

    def load_turns(self, limit: int | None = None) -> list[ConversationTurn]:
        if limit is not None and (not isinstance(limit, int) or limit < 0):
            raise ValueError("archive limit must be a nonnegative integer")
        with self._lock:
            turns: deque[ConversationTurn] = deque(maxlen=limit)
            seen = set()
            for path in self._paths():
                turn = self._read(path)
                if turn.turn_id in seen:
                    raise ValueError("malformed archive: duplicate turn IDs")
                seen.add(turn.turn_id)
                turns.append(turn)
            return list(turns)

    def read_turn(self, turn_id: str) -> ConversationTurn:
        digest = self._digest(turn_id)
        with self._lock:
            paths = [path for path in self._paths() if path.name.endswith(f"-{digest}.json")]
            if len(paths) > 1:
                raise ValueError("malformed archive: duplicate turn IDs")
            if not paths:
                raise KeyError(turn_id)
            return self._read(paths[0])


    def _artifact_directory(self) -> Path:
        directory = self._directory.parent / "artifacts"
        self._check_path(directory)
        directory.mkdir(exist_ok=True)
        self._check_path(directory)
        return directory

    def _artifact_path(self, ref: str) -> Path:
        if not isinstance(ref, str):
            raise ValueError("invalid archive reference")
        match = re.fullmatch(r"ctx:([0-9a-f]{64}):(tool|summary):([0-9a-f]{32})", ref)
        if match is None or match[1] != self._session_digest:
            raise ValueError("invalid or foreign archive reference")
        path = self._artifact_directory() / f"{match[2]}-{match[3]}.json"
        self._check_path(path)
        return path

    def _publish_artifact(self, kind: str, text: str, metadata: dict) -> str:
        if kind not in {"tool", "summary"} or not isinstance(text, str):
            raise ValueError("invalid archive text or kind")
        identity = uuid4().hex
        ref = f"ctx:{self._session_digest}:{kind}:{identity}"
        path = self._artifact_path(ref)
        record = {"version": 1, "session_id": self.session_id, "ref": ref,
                  "kind": kind, "text": text, "metadata": metadata,
                  "created_at": datetime.now(UTC).isoformat()}
        # Encode before creating a temporary file; invalid metadata never publishes.
        payload = json.dumps(record, ensure_ascii=False, sort_keys=True)
        fd, temporary = tempfile.mkstemp(prefix=".artifact-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            self._check_path(path)
            if path.exists():
                raise ValueError("archive artifact identity conflict")
            os.replace(temporary, path)
            if os.name != "nt":
                directory_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return ref

    def _read_artifact(self, ref: str) -> dict:
        path = self._artifact_path(ref)
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if (set(record) != {"version", "session_id", "ref", "kind", "text", "metadata", "created_at"}
                    or record["version"] != 1 or record["session_id"] != self.session_id
                    or record["ref"] != ref or record["kind"] != ref.split(":")[2]
                    or not isinstance(record["text"], str) or not isinstance(record["metadata"], dict)):
                raise ValueError("invalid artifact metadata")
            datetime.fromisoformat(record["created_at"])
            return record
        except FileNotFoundError as exc:
            raise KeyError(ref) from exc
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise ValueError("malformed archive artifact") from exc

    def save_text(self, text: str, *, kind: str = "tool", metadata: dict | None = None) -> str:
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError("artifact metadata must be a dictionary")
        with self._lock:
            return self._publish_artifact(kind, text, metadata or {})

    def read_text(self, ref: str, offset: int = 0, limit: int = 8000) -> str:
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 0 <= limit <= 16000:
            raise ValueError("invalid archive text offset or limit")
        with self._lock:
            return self._read_artifact(ref)["text"][offset:offset + limit]

    def save_summary(self, turn_ids, body: str, title: str, description: str,
                     *, committed: bool = True) -> SummaryEntry:
        ids = tuple(turn_ids)
        if (not ids or len(set(ids)) != len(ids) or not isinstance(body, str) or not body.strip()
                or not isinstance(title, str) or not title.strip()
                or not isinstance(description, str) or not description.strip()):
            raise ValueError("invalid summary metadata")
        with self._lock:
            for turn_id in ids:
                self.read_turn(turn_id)
            memory_id = uuid4().hex
            ref = self._publish_artifact("summary", body, {
                "memory_id": memory_id, "title": title, "description": description,
                "source_turn_ids": list(ids),
            })
            entry = SummaryEntry(memory_id, title, description, ref, ids, self.session_id)
            if committed:
                self.commit_summary(entry)
            return entry

    def commit_summary(self, entry: SummaryEntry) -> None:
        """Atomically expose a staged summary through a small durable marker.

        Memory calls this synchronously after its revision check. The brief
        marker fsync is intentional: clear/cancel cannot interleave with commit.
        Staged records abandoned after cancellation remain invisible to reload.
        """
        if (not isinstance(entry, SummaryEntry) or entry.session_id != self.session_id
                or not re.fullmatch(r"[0-9a-f]{32}", entry.memory_id)
                or not entry.storage_ref.startswith(f"ctx:{self._session_digest}:summary:")):
            raise ValueError("invalid summary commit entry")
        with self._lock:
            source = self._artifact_path(entry.storage_ref)
            if not source.is_file():
                raise KeyError(entry.storage_ref)
            marker = source.with_suffix(".commit")
            self._check_path(marker)
            payload = json.dumps({"memory_id": entry.memory_id, "storage_ref": entry.storage_ref}, sort_keys=True)
            if marker.exists():
                if marker.read_text(encoding="utf-8") != payload:
                    raise ValueError("summary commit conflict")
                return
            fd, temporary = tempfile.mkstemp(prefix=".commit-", suffix=".tmp", dir=marker.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                self._check_path(marker)
                os.replace(temporary, marker)
                if os.name != "nt":
                    directory_fd = os.open(marker.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
            finally:
                Path(temporary).unlink(missing_ok=True)

    def _summary_entry(self, record: dict) -> SummaryEntry:
        meta = record["metadata"]
        try:
            if (set(meta) != {"memory_id", "title", "description", "source_turn_ids"}
                    or not re.fullmatch(r"[0-9a-f]{32}", meta["memory_id"])
                    or not all(isinstance(meta[key], str) and meta[key].strip() for key in ("title", "description"))
                    or not isinstance(meta["source_turn_ids"], list) or not meta["source_turn_ids"]
                    or not all(isinstance(value, str) and value for value in meta["source_turn_ids"])
                    or len(set(meta["source_turn_ids"])) != len(meta["source_turn_ids"])):
                raise ValueError("invalid summary metadata")
            return SummaryEntry(meta["memory_id"], meta["title"], meta["description"],
                                record["ref"], tuple(meta["source_turn_ids"]), self.session_id)
        except (KeyError, TypeError) as exc:
            raise ValueError("invalid summary metadata") from exc

    def list_summaries(self) -> list[SummaryEntry]:
        with self._lock:
            records = []
            for path in self._artifact_directory().glob("summary-*.commit"):
                self._check_path(path)
                if not re.fullmatch(r"summary-[0-9a-f]{32}\.commit", path.name):
                    raise ValueError("invalid summary commit filename")
                ref = f"ctx:{self._session_digest}:summary:{path.stem[8:]}"
                try:
                    marker = json.loads(path.read_text(encoding="utf-8"))
                    record = self._read_artifact(ref)
                    entry = self._summary_entry(record)
                    if marker != {"memory_id": entry.memory_id, "storage_ref": ref}:
                        raise ValueError("invalid summary commit marker")
                except (KeyError, TypeError) as exc:
                    raise ValueError("invalid summary commit marker") from exc
                records.append(record)
            records.sort(key=lambda record: (record["created_at"], record["ref"]))
            return [self._summary_entry(record) for record in records]

    def read_summary(self, memory_id: str) -> str:
        with self._lock:
            for entry in self.list_summaries():
                if entry.memory_id == memory_id:
                    return self._read_artifact(entry.storage_ref)["text"]
            raise KeyError(memory_id)

    def pending_summary_turns(self, covered: set[str], live_limit: int,
                              batch_limit: int, token_budget: int) -> tuple[list[ConversationTurn], int]:
        """Scan durable originals while retaining only one bounded raw batch."""
        with self._lock:
            paths = self._paths()
            selected = []
            selected_tokens = 0
            pending_tokens = 0
            for path in paths[:-live_limit]:
                turn = self._read(path)
                if turn.turn_id in covered:
                    continue
                tokens = ConversationMemory._estimate_turn_tokens(turn)
                pending_tokens += tokens
                if len(selected) < batch_limit and selected_tokens + tokens <= token_budget:
                    selected.append(turn)
                    selected_tokens += tokens
            return selected, pending_tokens


    def find_turns(self, query: str, limit: int = 3) -> list[dict]:
        """Lexically scan originals, retaining at most 32 small source records.

        Unlike the summary batch, this includes incompressible/oversized turns.
        Call through to_thread: scans load one complete original at a time.
        """
        if not isinstance(query, str) or type(limit) is not int or not 0 <= limit <= 32:
            raise ValueError("invalid raw-source query or limit")
        terms = set(re.findall(r"\w+", query.casefold()))
        if not terms or limit == 0:
            return []
        ranked = []
        with self._lock:
            for sequence, path in enumerate(self._paths()):
                turn = self._read(path)
                score = sum(any(term in message.content.casefold() for message in turn.messages)
                            for term in terms)
                if not score:
                    continue
                description = " ".join(message.content[:200] for message in turn.messages[:2])[:200]
                entry = {"turn_id": turn.turn_id, "description": description,
                         "storage_ref": "turn:" + turn.turn_id}
                ranked.append((score, sequence, entry))
                ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
                del ranked[limit:]
        return [entry for _, _, entry in ranked]
