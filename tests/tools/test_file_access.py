import codecs

import pytest

from app.tools.filesystem.file_access import FileAccess


TEXT = "Hello, \u4e2d\u6587!"


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16", "utf-32"])
def test_auto_reads_english_and_chinese(tmp_path, encoding):
    (tmp_path / "sample.txt").write_bytes(TEXT.encode(encoding))
    assert FileAccess(tmp_path).read_file("sample.txt") == TEXT


@pytest.mark.parametrize("bom,encoding", [
    (codecs.BOM_UTF16_BE, "utf-16-be"),
    (codecs.BOM_UTF32_BE, "utf-32-be"),
])
def test_auto_reads_big_endian_bom(tmp_path, bom, encoding):
    (tmp_path / "sample.txt").write_bytes(bom + TEXT.encode(encoding))
    assert FileAccess(tmp_path).read_file("sample.txt") == TEXT


@pytest.mark.parametrize("encoding", ["gbk", "gb18030"])
def test_legacy_encoding_is_automatic(tmp_path, encoding):
    (tmp_path / "sample.txt").write_bytes(TEXT.encode(encoding))
    access = FileAccess(tmp_path)
    assert access.read_file("sample.txt") == TEXT


def test_byte_limit_still_applies(tmp_path):
    data = TEXT.encode("utf-8")
    (tmp_path / "sample.txt").write_bytes(data)
    assert FileAccess(tmp_path, len(data)).read_file("sample.txt") == TEXT
    with pytest.raises(ValueError, match="exceeds"):
        FileAccess(tmp_path, len(data) - 1).read_file("sample.txt")


def test_empty_file(tmp_path):
    (tmp_path / "empty.txt").write_bytes(b"")
    access = FileAccess(tmp_path)
    assert access.read_file("empty.txt") == ""


def test_malformed_data_is_not_silently_replaced(tmp_path):
    (tmp_path / "sample.txt").write_bytes(b"hello\xff")
    with pytest.raises(ValueError, match="cannot decode"):
        FileAccess(tmp_path).read_file("sample.txt")


@pytest.mark.parametrize("data", [
    codecs.BOM_UTF8 + b"\xd6\xd0",
    codecs.BOM_UTF16_LE + b"a",
    codecs.BOM_UTF32_LE + b"a",
])
def test_invalid_bom_payload_does_not_fall_back(tmp_path, data):
    (tmp_path / "sample.txt").write_bytes(data)
    with pytest.raises(ValueError, match="cannot decode"):
        FileAccess(tmp_path).read_file("sample.txt")


def test_gb18030_four_byte_character(tmp_path):
    text = "hello \U0001f600"
    (tmp_path / "sample.txt").write_bytes(text.encode("gb18030"))
    assert FileAccess(tmp_path).read_file("sample.txt") == text



def test_write_root_is_separate_and_validation_does_not_write(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    access = FileAccess(tmp_path, write_root=output, max_write_bytes=100)
    target = access.resolve_write_path("new.txt")
    assert target == output / "new.txt"
    assert not target.exists()
    target.write_text("old", encoding="utf-8")
    assert access.resolve_write_path("new.txt") == target
    assert target.read_text(encoding="utf-8") == "old"
    assert access._max_write_bytes == 100


def test_writing_disabled_by_default(tmp_path):
    with pytest.raises(PermissionError, match="disabled"):
        FileAccess(tmp_path).resolve_write_path("new.txt")


@pytest.mark.parametrize("path", [
    "", " ", ".", "../outside.txt", "a/../b", "C:/outside.txt", "C:outside.txt",
    "file:stream", "file\x00", "NUL", "CON.txt", "COM1.log", "LPT1",
    "file.", "file ", "a?b", "folder/",
])
def test_invalid_write_paths(tmp_path, path):
    with pytest.raises(ValueError):
        FileAccess(tmp_path, write_root=tmp_path).resolve_write_path(path)


def test_write_parent_and_target_types(tmp_path):
    access = FileAccess(tmp_path, write_root=tmp_path)
    with pytest.raises(FileNotFoundError):
        access.resolve_write_path("missing/file.txt")
    assert not (tmp_path / "missing").exists()
    (tmp_path / "directory").mkdir()
    with pytest.raises(ValueError):
        access.resolve_write_path("directory")
    (tmp_path / "file.txt").write_text("test")
    with pytest.raises(NotADirectoryError):
        access.resolve_write_path("file.txt/child.txt")
    with pytest.raises(TypeError):
        access.resolve_write_path(None)


@pytest.mark.parametrize("limit,error", [(True, TypeError), (1.5, TypeError), (0, ValueError), (-1, ValueError)])
def test_invalid_write_limit(tmp_path, limit, error):
    with pytest.raises(error):
        FileAccess(tmp_path, max_write_bytes=limit)


def test_invalid_write_root(tmp_path):
    with pytest.raises(TypeError):
        FileAccess(tmp_path, write_root=str(tmp_path))
    with pytest.raises(ValueError):
        FileAccess(tmp_path, write_root=type(tmp_path)("relative"))
    with pytest.raises(FileNotFoundError):
        FileAccess(tmp_path, write_root=tmp_path / "missing")
    file = tmp_path / "file"
    file.write_text("test")
    with pytest.raises(NotADirectoryError):
        FileAccess(tmp_path, write_root=file)


def test_write_rejects_symlinks_including_dangling(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (root / "link").symlink_to(outside, target_is_directory=True)
        (root / "dangling").symlink_to(outside / "missing")
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    access = FileAccess(root, write_root=root)
    for path in ("link/new.txt", "dangling"):
        with pytest.raises(PermissionError, match="links"):
            access.resolve_write_path(path)


@pytest.mark.parametrize("overwrite", [False, True])
@pytest.mark.parametrize("content", [TEXT, "", "  "])
def test_write_new_file_and_read_back(tmp_path, overwrite, content):
    access = FileAccess(tmp_path, write_root=tmp_path)
    access.write_file("new.txt", content, overwrite=overwrite)
    assert (tmp_path / "new.txt").read_bytes() == content.encode("utf-8")
    assert access.read_file("new.txt") == content
    assert sorted(p.name for p in tmp_path.iterdir()) == ["new.txt"]


def test_write_existing_without_overwrite_preserves_original(tmp_path):
    target = tmp_path / "existing.txt"
    target.write_bytes(b"original")
    with pytest.raises(FileExistsError):
        FileAccess(tmp_path, write_root=tmp_path).write_file("existing.txt", TEXT)
    assert target.read_bytes() == b"original"


def test_write_overwrite_replaces_content(tmp_path):
    target = tmp_path / "existing.txt"
    target.write_bytes(b"original")
    FileAccess(tmp_path, write_root=tmp_path).write_file("existing.txt", TEXT, overwrite=True)
    assert target.read_bytes() == TEXT.encode("utf-8")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["existing.txt"]


@pytest.mark.parametrize("content,overwrite", [(None, False), (123, False), ("text", 1)])
def test_write_rejects_invalid_types_without_creating_files(tmp_path, content, overwrite):
    with pytest.raises(TypeError):
        FileAccess(tmp_path, write_root=tmp_path).write_file("new.txt", content, overwrite=overwrite)
    assert list(tmp_path.iterdir()) == []


def test_write_byte_limit_preserves_original(tmp_path):
    target = tmp_path / "existing.txt"
    target.write_bytes(b"old")
    access = FileAccess(tmp_path, write_root=tmp_path, max_write_bytes=len(TEXT.encode("utf-8")) - 1)
    with pytest.raises(ValueError, match="exceeds"):
        access.write_file("existing.txt", TEXT, overwrite=True)
    assert target.read_bytes() == b"old"


@pytest.mark.parametrize("path,error", [("../outside.txt", ValueError), ("missing/file.txt", FileNotFoundError)])
def test_write_rejects_invalid_destination(tmp_path, path, error):
    with pytest.raises(error):
        FileAccess(tmp_path, write_root=tmp_path).write_file(path, "text")
    assert list(tmp_path.iterdir()) == []


def test_write_disabled_does_not_create_files(tmp_path):
    with pytest.raises(PermissionError):
        FileAccess(tmp_path).write_file("new.txt", "text")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("failure_stage", ["flush_to_disk", "replace"])
def test_overwrite_failure_preserves_original_and_cleans_temp(tmp_path, monkeypatch, failure_stage):
    import app.tools.filesystem.file_access as module
    target = tmp_path / "existing.txt"
    target.write_bytes(b"original")
    invoked = []
    def fail(*args, **kwargs):
        invoked.append(True)
        raise OSError("injected failure")
    if failure_stage == "flush_to_disk":
        monkeypatch.setattr(module.os, "fsync", fail)
    else:
        monkeypatch.setattr(module.Path, "replace", fail)
    with pytest.raises(OSError, match="injected failure"):
        FileAccess(tmp_path, write_root=tmp_path).write_file("existing.txt", TEXT, overwrite=True)
    assert invoked
    assert target.read_bytes() == b"original"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["existing.txt"]
