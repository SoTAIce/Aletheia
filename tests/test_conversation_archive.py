from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone, timedelta

import pytest

from app.models.conversation_memory import ConversationMessage, ConversationTurn, MessageRole


def archive_type():
    from app.services import conversation_archive
    return conversation_archive.ConversationArchive


def native_test_path(path):
    """Allow corruption fixtures to touch files past Windows MAX_PATH too."""
    import os
    from pathlib import Path
    if os.name == "nt":
        value = str(path.absolute())
        return Path("\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value)
    return path


def turn(index=0):
    stamp = datetime(2025, 1, 2, 3, 4, 5, 123456, tzinfo=timezone(timedelta(hours=8)))
    return ConversationTurn(
        [ConversationMessage(role, f"正文\n{role.value}-{index}", stamp) for role in MessageRole],
        turn_id=f"turn/{index}", created_at=stamp,
    )


def test_roundtrip_retains_order_complete_messages_and_recent_limit(tmp_path):
    archive = archive_type()(tmp_path, "session-a")
    turns = [turn(i) for i in range(4)]
    for item in turns:
        archive.save_turn(item)
    reopened = archive_type()(tmp_path, "session-a")
    assert reopened.load_turns() == turns
    assert reopened.load_turns(limit=2) == turns[-2:]
    assert reopened.load_turns(limit=0) == []
    assert reopened.read_turn("turn/1") == turns[1]
    with pytest.raises(KeyError):
        reopened.read_turn("missing")
    with pytest.raises(ValueError):
        reopened.load_turns(limit=-1)


def test_idempotence_conflict_and_session_isolation(tmp_path):
    archive = archive_type()(tmp_path, "a")
    item = turn()
    with ThreadPoolExecutor() as executor:
        list(executor.map(archive.save_turn, [item] * 12))
    assert archive.load_turns() == [item]
    with pytest.raises(ValueError, match="conflict"):
        archive.save_turn(replace(item, messages=[ConversationMessage(MessageRole.USER, "changed")]))
    assert archive_type()(tmp_path, "b").load_turns() == []
    assert archive.read_turn(item.turn_id) == item


@pytest.mark.parametrize("session", ["", None, 123])
def test_invalid_session_id_is_rejected(tmp_path, session):
    with pytest.raises(ValueError):
        archive_type()(tmp_path, session)


@pytest.mark.parametrize("session", ["中文会话", "../escape", "..", "a/b", "a\\b", "/absolute", "id: with spaces", "long" * 100])
def test_arbitrary_nonempty_sessions_are_isolated_inside_hashed_directory(tmp_path, session):
    import hashlib
    archive = archive_type()(tmp_path, session)
    archive.save_turn(turn())
    assert archive.session_id == session
    assert archive_type()(tmp_path, session).load_turns() == [turn()]
    assert archive_type()(tmp_path, session + "-other").load_turns() == []
    path = next(tmp_path.rglob("*.json"))
    expected_name = "session-" + hashlib.sha256(session.encode("utf-8")).hexdigest()
    assert path.relative_to(tmp_path).parts == (expected_name, "turns", path.name)


def test_malformed_records_are_not_silently_skipped(tmp_path):
    archive = archive_type()(tmp_path, "a")
    archive.save_turn(turn())
    path = next(native_test_path(tmp_path).rglob("*.json"))
    path.write_text('{"messages": []}', encoding="utf-8")
    with pytest.raises(ValueError, match="archive"):
        archive.load_turns()
    with pytest.raises(ValueError, match="archive"):
        archive.read_turn("turn/0")


def test_failed_publication_retains_original_and_no_partial_record(tmp_path, monkeypatch):
    import os
    archive = archive_type()(tmp_path, "a")
    archive.save_turn(turn())
    def fail(*args, **kwargs):
        raise OSError("disk unavailable")
    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        archive.save_turn(turn(1))
    assert archive.load_turns() == [turn()]
    assert len(list(tmp_path.rglob("*.json"))) == 1


def test_symlink_record_escape_is_rejected(tmp_path):
    archive = archive_type()(tmp_path / "archive", "a")
    archive.save_turn(turn())
    record = next(native_test_path(tmp_path / "archive").rglob("*.json"))
    outside = native_test_path(tmp_path / "outside.json")
    outside.write_bytes(record.read_bytes())
    record.unlink()
    try:
        record.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation not permitted")
    with pytest.raises(ValueError):
        archive.load_turns()


def test_duplicate_sequence_is_malformed_not_ambiguous_order(tmp_path):
    archive = archive_type()(tmp_path, "a")
    archive.save_turn(turn(0))
    archive.save_turn(turn(1))
    first, second = sorted(native_test_path(tmp_path).rglob("*.json"))
    second.rename(second.with_name(first.name[:20] + second.name[20:]))
    with pytest.raises(ValueError, match="archive"):
        archive.load_turns()


def test_foreign_session_record_is_rejected(tmp_path):
    import json
    first = archive_type()(tmp_path, "a")
    second = archive_type()(tmp_path, "b")
    first.save_turn(turn())
    second.save_turn(turn())
    paths = {json.loads(path.read_text(encoding="utf-8"))["session_id"]: path
             for path in native_test_path(tmp_path).rglob("*.json")}
    first_path, second_path = paths["a"], paths["b"]
    second_path.write_bytes(first_path.read_bytes())
    with pytest.raises(ValueError, match="archive"):
        second.read_turn("turn/0")


def test_fsync_failure_does_not_publish_turn(tmp_path, monkeypatch):
    import os
    archive = archive_type()(tmp_path, "a")
    def fail(fd):
        raise OSError("flush failed")
    monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(OSError, match="flush failed"):
        archive.save_turn(turn())
    assert archive.load_turns() == []
    assert not list(tmp_path.rglob("*.tmp"))


def test_deep_storage_root_supports_atomic_publication_and_reopen(tmp_path):
    # The final record exceeds MAX_PATH even with a short pytest basetemp.
    root = tmp_path / ("deep-root-" + "x" * 110) / ("nested-" + "y" * 50)
    archive = archive_type()(root, "中文 session")
    archive.save_turn(turn())
    archive.save_turn(turn(1))
    assert archive.root == root.absolute()
    reopened = archive_type()(root, "中文 session")
    assert reopened.load_turns() == [turn(), turn(1)]
    assert reopened.read_turn("turn/0") == turn()


def test_windows_reparse_point_rejected_without_path_is_junction(tmp_path, monkeypatch):
    import os
    import stat
    from pathlib import Path
    from types import SimpleNamespace
    if os.name != "nt":
        pytest.skip("Windows reparse attributes")
    archive_class = archive_type()
    monkeypatch.delattr(Path, "is_junction", raising=False)
    lstat = Path.lstat
    def reparse_lstat(path, *args, **kwargs):
        result = lstat(path, *args, **kwargs)
        if path == tmp_path:
            return SimpleNamespace(st_mode=result.st_mode,
                                   st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        return result
    monkeypatch.setattr(Path, "lstat", reparse_lstat)
    with pytest.raises(ValueError, match="archive path"):
        archive_class._check_path(tmp_path)
