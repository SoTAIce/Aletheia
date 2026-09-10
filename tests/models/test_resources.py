from uuid import UUID

import pytest

from app.models.resources import Resources, ResourceType


def test_resource_lifecycle() -> None:
    resources = Resources()
    first = resources.register(' first.txt ', ResourceType.FILE, ' upload ')
    second = resources.register('second.txt', ResourceType.ARTIFACT)
    assert first != second
    assert UUID(first).version == UUID(second).version == 4
    original = resources.get(first)
    assert original.ref == 'first.txt'
    assert original.source == 'upload'
    assert not original.loaded
    assert original.summary is original.selected_context is None
    assert original.created_at == original.updated_at
    assert original.created_at.utcoffset().total_seconds() == 0
    assert resources.get(second).source is None

    resources.select_context(f' {first} ', ' context ')
    resources.mark_loaded(f' {first} ', ' summary ')
    loaded = resources.get(first)
    assert loaded.loaded and loaded.summary == 'summary'
    assert loaded.selected_context == 'context'
    assert loaded.created_at == original.created_at
    assert loaded.updated_at >= original.updated_at
    assert not original.loaded and original.selected_context is None
    assert resources.list_loaded() == [loaded]
    assert resources.list_selected() == [loaded]
    snapshot = resources.list_all()
    assert [item.resource_id for item in snapshot] == [first, second]
    snapshot.clear()
    assert len(resources.list_all()) == 2

    with pytest.raises(RuntimeError):
        resources.mark_loaded(first)
    assert resources.get(first) is loaded
    resources.clear_selected_context(f' {first} ')
    cleared = resources.get(first)
    assert cleared.selected_context is None
    assert cleared.loaded and cleared.summary == 'summary'
    resources.clear_selected_context(first)
    assert resources.get(first) is cleared
    resources.select_context(first, 'again')
    resources.select_context(second, 'another')
    assert resources.clear_all_selected_contexts() == 2
    assert resources.list_selected() == []
    assert resources.clear_all_selected_contexts() == 0
    assert resources.remove(f' {first} ').resource_id == first
    resources.mark_loaded(second)
    assert resources.get(second).summary is None


@pytest.mark.parametrize('invalid', [None, '', '  ', 42])
def test_registration_rejects_invalid_ref(invalid) -> None:
    resources = Resources()
    with pytest.raises(ValueError):
        resources.register(invalid, ResourceType.FILE)
    assert resources.list_all() == []


@pytest.mark.parametrize('invalid', ['', '  ', 42])
def test_registration_rejects_invalid_source(invalid) -> None:
    with pytest.raises(ValueError):
        Resources().register('file.txt', ResourceType.FILE, invalid)


def test_registration_types_and_duplicate_refs() -> None:
    resources = Resources()
    for kind in ResourceType:
        resource_id = resources.register(kind.value, kind)
        assert resources.get(resource_id).resource_type is kind
    with pytest.raises(TypeError):
        resources.register('other', 'file')
    with pytest.raises(ValueError):
        resources.register(' file ', ResourceType.ARTIFACT)
    assert len(resources.list_all()) == len(ResourceType)


@pytest.mark.parametrize('method,args', [
    ('get', ()), ('remove', ()), ('mark_loaded', ()),
    ('select_context', ('text',)), ('clear_selected_context', ()),
])
def test_resource_id_validation(method, args) -> None:
    resources = Resources()
    for invalid in (None, '', '  ', 42):
        with pytest.raises(ValueError):
            getattr(resources, method)(invalid, *args)
    for populated in (False, True):
        if populated:
            resources.register('file.txt', ResourceType.FILE)
        with pytest.raises(KeyError, match='does not exist'):
            getattr(resources, method)('missing', *args)


def test_invalid_updates_preserve_resource() -> None:
    resources = Resources()
    resource_id = resources.register('file.txt', ResourceType.FILE)
    original = resources.get(resource_id)
    for invalid in ('', '  ', 42):
        with pytest.raises(ValueError):
            resources.mark_loaded(resource_id, invalid)
        assert resources.get(resource_id) is original
    for invalid in (None, '', '  ', 42):
        with pytest.raises(ValueError):
            resources.select_context(resource_id, invalid)
        assert resources.get(resource_id) is original


def test_empty_resources() -> None:
    resources = Resources()
    assert resources.list_all() == []
    assert resources.list_loaded() == []
    assert resources.list_selected() == []
    assert resources.clear_all_selected_contexts() == 0
