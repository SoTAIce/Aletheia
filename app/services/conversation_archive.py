"""Immutable, session-bound conversation originals for a single process."""
from __future__ import annotations

import hashlib
from collections import deque
from dataclasses import replace
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
from datetime import UTC, datetime
from uuid import uuid4

from app.models.conversation_memory import ConversationMessage, ConversationTurn, MessageRole, SummaryEntry, ConversationMemory, TurnStatus


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
        # Derived, body-free metadata only. Rebuild after restart; never authoritative.
        self._turn_metadata: dict[str, tuple[tuple, str, int, TurnStatus | None, str | None]] = {}

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
        record = {"version": 1, "session_id": self.session_id, "turn_id": turn.turn_id,
                  "created_at": turn.created_at.isoformat(), "messages": messages}
        if turn.status is not None or turn.task_id is not None:
            if (not isinstance(turn.task_id, str) or not turn.task_id.strip()
                    or not isinstance(turn.status, TurnStatus)
                    or (turn.question_id is not None and (not isinstance(turn.question_id, str) or not turn.question_id))):
                raise ValueError('invalid turn lifecycle metadata')
            if (turn.created_at.utcoffset() is None or messages[0]['role'] != 'user'
                    or len(messages) != (1 if turn.status is TurnStatus.RECEIVED else 2)):
                raise ValueError('invalid turn lifecycle messages')
            if turn.status is TurnStatus.RECEIVED:
                if turn.finished_at is not None:
                    raise ValueError('received turn cannot be finished')
            elif (not isinstance(turn.finished_at, datetime) or turn.finished_at.utcoffset() is None
                   or turn.finished_at < turn.created_at or messages[-1]['role'] != 'assistant'):
                raise ValueError('invalid turn completion time or reply')
            record.update(version=2, task_id=turn.task_id, status=turn.status.value,
                          finished_at=turn.finished_at.isoformat() if turn.finished_at else None,
                          question_id=turn.question_id)
        elif turn.finished_at is not None or turn.question_id is not None:
            raise ValueError('legacy turn cannot carry lifecycle metadata')
        return record

    def _decode_record(self, data: dict) -> ConversationTurn:
        base = {'version', 'session_id', 'turn_id', 'created_at', 'messages'}
        extra = {'task_id', 'status', 'finished_at', 'question_id'}
        if (not isinstance(data, dict) or data.get('version') not in (1, 2)
                or set(data) != (base if data['version'] == 1 else base | extra)
                or data['session_id'] != self.session_id):
            raise ValueError('invalid record metadata')
        messages = []
        for item in data['messages']:
            if set(item) != {'role', 'content', 'created_at'} or not isinstance(item['content'], str):
                raise ValueError('invalid message')
            messages.append(ConversationMessage(MessageRole(item['role']), item['content'],
                                                datetime.fromisoformat(item['created_at'])))
        turn = ConversationTurn(messages, data['turn_id'], datetime.fromisoformat(data['created_at']))
        if data['version'] == 2:
            turn.task_id, turn.status, turn.question_id = data['task_id'], TurnStatus(data['status']), data['question_id']
            turn.finished_at = datetime.fromisoformat(data['finished_at']) if data['finished_at'] is not None else None
        self._record(turn)
        return turn

    def _read_original(self, path: Path) -> ConversationTurn:
        self._check_path(path)
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            turn = self._decode_record(data)
            if path.name[21:] != f"{self._digest(turn.turn_id)}.json" or not path.name[:20].isdigit():
                raise ValueError('invalid record identity')
            return turn
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise ValueError(f'malformed archive record: {path.name}') from exc

    def _read(self, path: Path) -> ConversationTurn:
        original = self._read_original(path)
        outcome = self._read_outcome(original)
        return self._decode_record(outcome['turn']) if outcome is not None else original

    def _outcome_path(self, turn_id: str) -> Path:
        path = self._directory.parent / 'outcomes' / f'{self._digest(turn_id)}.json'
        self._check_path(path)
        return path

    def _read_outcome(self, original: ConversationTurn) -> dict | None:
        path = self._outcome_path(original.turn_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            if (set(data) != {'version', 'session_id', 'turn', 'result'} or data['version'] != 1
                    or data['session_id'] != self.session_id or original.status is not TurnStatus.RECEIVED
                    or (data['result'] is not None and not isinstance(data['result'], dict))):
                raise ValueError('invalid outcome metadata')
            final = self._decode_record(data['turn'])
            if (final.status in (None, TurnStatus.RECEIVED) or final.turn_id != original.turn_id
                    or final.task_id != original.task_id or final.question_id != original.question_id
                    or final.created_at != original.created_at or final.messages[:-1] != original.messages):
                raise ValueError('outcome does not match original')
            return data
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise ValueError('malformed archive outcome') from exc

    def complete_turn(self, turn: ConversationTurn, *, result: dict | None = None) -> ConversationTurn:
        record = self._record(turn)
        payload = json.dumps({'version': 1, 'session_id': self.session_id, 'turn': record,
                              'result': result}, ensure_ascii=False, sort_keys=True, allow_nan=False)
        if turn.status in (None, TurnStatus.RECEIVED) or (result is not None and not isinstance(result, dict)):
            raise ValueError('invalid turn outcome')
        # Validate and publish the same detached snapshot even if the caller
        # mutates its dataclass while waiting for the archive lock.
        turn = self._decode_record(record)
        with self._lock:
            paths = [p for p in self._paths() if p.name.endswith(f'-{self._digest(turn.turn_id)}.json')]
            if len(paths) != 1:
                raise KeyError(turn.turn_id)
            original = self._read_original(paths[0])
            if (original.status is not TurnStatus.RECEIVED or turn.task_id != original.task_id
                    or turn.question_id != original.question_id or turn.created_at != original.created_at
                    or turn.messages[:-1] != original.messages):
                raise ValueError('turn outcome conflict')
            path = self._outcome_path(turn.turn_id)
            existing = self._read_outcome(original)
            if existing is not None:
                committed = self._decode_record(existing['turn'])
                # Generated completion timestamps are not request identity.
                # Concurrent identical outcomes keep the first durable time.
                comparable = replace(committed, finished_at=turn.finished_at,
                                     messages=[*committed.messages[:-1], replace(
                                         committed.messages[-1], created_at=turn.messages[-1].created_at)])
                if self._record(comparable) != record or existing['result'] != json.loads(payload)['result']:
                    raise ValueError('turn outcome conflict')
                return committed
            path.parent.mkdir(exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix='.outcome-', suffix='.tmp', dir=path.parent)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                self._check_path(path)
                os.replace(temporary, path)
                if os.name != 'nt':
                    directory_fd = os.open(path.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
            finally:
                Path(temporary).unlink(missing_ok=True)
            return turn

    def read_turn_result(self, turn_id: str) -> dict | None:
        with self._lock:
            paths = [p for p in self._paths() if p.name.endswith(f'-{self._digest(turn_id)}.json')]
            if len(paths) != 1:
                raise KeyError(turn_id)
            outcome = self._read_outcome(self._read_original(paths[0]))
            return outcome['result'] if outcome else None

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

    @staticmethod
    def _fingerprint(path: Path) -> tuple:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0):
            raise ValueError('archive record must not be a symlink or reparse point')
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                info.st_ctime_ns, info.st_mode, getattr(info, 'st_file_attributes', 0))

    def _validated_paths(self, paths: list[Path] | None = None,
                         loaded: dict[str, ConversationTurn | None] | None = None) -> list[Path]:
        """Audit names/stats every time; decode only new or changed originals.

        Caller holds the session lock. O(history) metadata work remains; we do
        not rely on directory mtime, which misses in-place record changes.
        Application-owned immutable files remain the security boundary.
        """
        paths = self._paths() if paths is None else paths
        metadata = {}
        seen = set()
        for path in paths:
            fingerprint = self._fingerprint(path)
            outcome_path = self._directory.parent / 'outcomes' / path.name[21:]
            self._check_path(outcome_path)
            fingerprint = (fingerprint, self._fingerprint(outcome_path) if outcome_path.exists() else None)
            entry = self._turn_metadata.get(path.name)
            if entry is None or entry[0] != fingerprint:
                turn = self._read(path)
                entry = (fingerprint, turn.turn_id, ConversationMemory._estimate_turn_tokens(turn),
                         turn.status, turn.task_id)
                if loaded is not None and path.name in loaded:
                    loaded[path.name] = turn
            if entry[1] in seen:
                raise ValueError('malformed archive: duplicate turn IDs')
            seen.add(entry[1])
            metadata[path.name] = entry
        self._turn_metadata = metadata
        return paths

    def save_turn(self, turn: ConversationTurn) -> None:
        record = self._record(turn)
        # Caller-owned dataclasses may change while this worker waits/writes.
        # Derive all identity and token metadata from the same durable snapshot.
        turn_id = record['turn_id']
        tokens = max(1, sum(4 + ConversationMemory._estimate_text_tokens(item['content'])
                            for item in record['messages']))
        with self._lock:
            paths = self._validated_paths()
            for path in paths:
                if self._turn_metadata[path.name][1] == turn_id:
                    if self._record(self._read_original(path)) != record:
                        raise ValueError(f"archive turn ID conflict: {turn_id}")
                    return
            sequence = int(paths[-1].name[:20]) + 1 if paths else 0
            destination = self._directory / f"{sequence:020d}-{self._digest(turn_id)}.json"
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
            # Publish metadata only after fsync/replace succeeded. If a late
            # directory fsync fails, the next audit rediscovers the durable file.
            self._turn_metadata[destination.name] = (
                (self._fingerprint(destination), None), turn_id, tokens,
                TurnStatus(record['status']) if record['version'] == 2 else None,
                record.get('task_id'),
            )

    def load_turns(self, limit: int | None = None) -> list[ConversationTurn]:
        if limit is not None and (not isinstance(limit, int) or limit < 0):
            raise ValueError("archive limit must be a nonnegative integer")
        with self._lock:
            if limit is not None:
                paths = self._paths()
                recent = paths[-limit:] if limit else []
                # Reuse bodies decoded by this cold/changed-file audit, bounded
                # by the requested window and discarded after the operation.
                loaded = dict.fromkeys(path.name for path in recent)
                self._validated_paths(paths, loaded)
                return [loaded[path.name] or self._read(path) for path in recent]
            # An unbounded read remains an explicit full-body audit, even when
            # filesystem metadata happens to be unchanged.
            turns: deque[ConversationTurn] = deque(maxlen=limit)
            seen = set()
            for path in self._paths():
                turn = self._read(path)
                if turn.turn_id in seen:
                    raise ValueError("malformed archive: duplicate turn IDs")
                seen.add(turn.turn_id)
                turns.append(turn)
            return list(turns)

    def _save_and_load_recent(self, turn: ConversationTurn, limit: int) -> list[ConversationTurn]:
        """One worker/lock scope; no await between publication and recent reload.

        Keep public operations (and their error behavior) intact. Each operation
        audits metadata; no shortcut weakens detection or survives an exception.
        """
        with self._lock:
            self.save_turn(turn)
            return self.load_turns(limit)

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
                     *, committed: bool = True, expected_turns=None) -> SummaryEntry:
        ids = tuple(turn_ids)
        if (not ids or len(set(ids)) != len(ids) or not isinstance(body, str) or not body.strip()
                or not isinstance(title, str) or not title.strip()
                or not isinstance(description, str) or not description.strip()):
            raise ValueError("invalid summary metadata")
        with self._lock:
            sources = [self.read_turn(turn_id) for turn_id in ids]
            if expected_turns is not None and tuple(sources) != tuple(expected_turns):
                raise ValueError('summary source snapshot changed')
            scopes = {turn.task_id for turn in sources}
            if any(turn.status is TurnStatus.RECEIVED for turn in sources) or len(scopes) != 1:
                raise ValueError('summary requires finished turns from one task (or legacy group)')
            task_id = sources[0].task_id
            memory_id = uuid4().hex
            metadata = {
                "memory_id": memory_id, "title": title, "description": description,
                "source_turn_ids": list(ids),
            }
            if task_id is not None:
                metadata['task_id'] = task_id
            ref = self._publish_artifact("summary", body, metadata)
            entry = SummaryEntry(memory_id, title, description, ref, ids, self.session_id, task_id)
            if committed:
                self.commit_summary(entry)
            return entry

    def commit_summary(self, entry: SummaryEntry, *, expected_turns=None) -> None:
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
            if expected_turns is not None:
                if (tuple(t.turn_id for t in expected_turns) != entry.source_turn_ids
                        or tuple(self.read_turn(t.turn_id) for t in expected_turns) != tuple(expected_turns)):
                    raise ValueError('summary source snapshot changed')
                if any(set(other.source_turn_ids) & set(entry.source_turn_ids)
                       for other in self.list_summaries() if other.memory_id != entry.memory_id):
                    raise ValueError('summary sources already covered')
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
            base = {"memory_id", "title", "description", "source_turn_ids"}
            if (set(meta) not in (base, base | {'task_id'})
                    or ('task_id' in meta and (not isinstance(meta['task_id'], str) or not meta['task_id'].strip()))
                    or not re.fullmatch(r"[0-9a-f]{32}", meta["memory_id"])
                    or not all(isinstance(meta[key], str) and meta[key].strip() for key in ("title", "description"))
                    or not isinstance(meta["source_turn_ids"], list) or not meta["source_turn_ids"]
                    or not all(isinstance(value, str) and value for value in meta["source_turn_ids"])
                    or len(set(meta["source_turn_ids"])) != len(meta["source_turn_ids"])):
                raise ValueError("invalid summary metadata")
            return SummaryEntry(meta["memory_id"], meta["title"], meta["description"],
                                record["ref"], tuple(meta["source_turn_ids"]), self.session_id, meta.get('task_id'))
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
            paths = self._validated_paths()
            selected = []
            selected_tokens = 0
            pending_tokens = 0
            scope_selected = False
            scope = None
            for path in paths[:-live_limit]:
                _, turn_id, tokens, status, task_id = self._turn_metadata[path.name]
                if turn_id in covered or status is TurnStatus.RECEIVED:
                    continue
                pending_tokens += tokens
                if (len(selected) < batch_limit and selected_tokens + tokens <= token_budget
                        and (not scope_selected or scope == task_id)):
                    selected.append(self._read(path))
                    selected_tokens += tokens
                    scope, scope_selected = task_id, True
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
