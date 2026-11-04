import asyncio
from copy import deepcopy
import threading
from unittest.mock import Mock, create_autospec

from langchain_core.embeddings import Embeddings
from pymilvus import DataType, MilvusClient
import pytest

from app.config import config
from app.services.memory_vector_index import MemoryVectorIndex


def schema():
    return {
        "auto_id": False,
        "fields": [
            {"name": "id", "type": DataType.VARCHAR, "is_primary": True,
             "params": {"max_length": 100}},
            {"name": "user_id", "type": DataType.VARCHAR, "params": {"max_length": 256}},
            {"name": "content", "type": DataType.VARCHAR, "params": {"max_length": 8000}},
            {"name": "vector", "type": DataType.FLOAT_VECTOR, "params": {"dim": 3}},
        ],
    }


@pytest.fixture
def setup_index(monkeypatch):
    monkeypatch.setattr(config, "embedding_dimensions", 3)
    client = create_autospec(MilvusClient, instance=True)
    client.has_collection.return_value = True
    client.describe_collection.return_value = schema()
    client.list_indexes.return_value = ["vector_idx"]
    client.describe_index.return_value = {
        "field_name": "vector", "index_type": "IVF_FLAT", "metric_type": "L2",
    }
    rows = {}

    def query(**kwargs):
        assert kwargs["filter"] == "id == {mid}"
        assert kwargs["consistency_level"] == "Strong"
        row = rows.get(kwargs["filter_params"]["mid"])
        return [{"user_id": row["user_id"]}] if row else []

    def upsert(**kwargs):
        for row in kwargs["data"]:
            rows[row["id"]] = deepcopy(row)
        return {"upsert_count": len(kwargs["data"])}

    def search(**kwargs):
        assert kwargs["filter"] == "user_id == {uid}"
        assert kwargs["consistency_level"] == "Strong"
        hits = [{"id": row["id"], "distance": sum(
            (a - b) ** 2 for a, b in zip(row["vector"], kwargs["data"][0])
        )} for row in rows.values() if row["user_id"] == kwargs["filter_params"]["uid"]]
        return [sorted(hits, key=lambda hit: hit["distance"])[:kwargs["limit"]]]

    def delete(**kwargs):
        assert kwargs["filter"] == "id == {mid} and user_id == {uid}"
        params = kwargs["filter_params"]
        row = rows.get(params["mid"])
        if row and row["user_id"] == params["uid"]:
            del rows[params["mid"]]
        return {"delete_count": 0}

    client.query.side_effect = query
    client.upsert.side_effect = upsert
    client.search.side_effect = search
    client.delete.side_effect = delete
    embeddings = create_autospec(Embeddings, instance=True)
    embeddings.embed_documents.return_value = [[1.0, 2.0, 3.0]]
    embeddings.embed_query.return_value = [1.0, 2.0, 3.0]
    return MemoryVectorIndex(client, embeddings), client, embeddings, rows


async def test_initialize_existing_is_idempotent_and_never_owns_connection(setup_index):
    index, client, _, _ = setup_index
    await asyncio.gather(index.initialize(), index.initialize())
    client.describe_collection.assert_called_once()
    client.load_collection.assert_called_once()
    client.create_collection.assert_not_called()
    client.create_index.assert_not_called()
    client.drop_collection.assert_not_called()
    client.release_collection.assert_not_called()
    client.close.assert_not_called()


async def test_create_missing_collection_and_index(setup_index):
    index, client, _, _ = setup_index
    client.has_collection.return_value = False
    client.list_indexes.return_value = []
    await index.initialize()
    created = client.create_collection.call_args.kwargs["schema"].to_dict()
    assert created["auto_id"] is False
    assert {f["name"] for f in created["fields"]} == {"id", "user_id", "content", "vector"}
    client.prepare_index_params.return_value.add_index.assert_called_once_with(
        field_name="vector", index_type="IVF_FLAT", metric_type="L2", params={"nlist": 128},
    )
    client.create_index.assert_called_once()
    client.load_collection.assert_called_once()


@pytest.mark.parametrize("mutation", [
    lambda s: s["fields"].pop(),
    lambda s: s.update(auto_id=True),
    lambda s: s["fields"][0].update(is_primary=False),
    lambda s: s["fields"][1].update(is_primary=True),
    lambda s: s["fields"][0].update(auto_id=True),
    lambda s: s["fields"][1].update(type=DataType.INT64),
    lambda s: s["fields"][3].update(type=DataType.BINARY_VECTOR),
    lambda s: s["fields"][3]["params"].update(dim=4),
    lambda s: s["fields"][2]["params"].update(max_length=10),
    lambda s: s["fields"][1].update(nullable=True),
    lambda s: s["fields"][1].update(is_partition_key=True),
])
async def test_reject_incompatible_schema_without_data_loss(setup_index, mutation):
    index, client, _, _ = setup_index
    mutation(client.describe_collection.return_value)
    with pytest.raises(RuntimeError):
        await index.initialize()
    assert not index._initialized
    client.load_collection.assert_not_called()
    client.drop_collection.assert_not_called()


@pytest.mark.parametrize("key,value", [("metric_type", "COSINE"), ("index_type", "HNSW")])
async def test_reject_incompatible_index(setup_index, key, value):
    index, client, _, _ = setup_index
    client.describe_index.return_value[key] = value
    with pytest.raises(RuntimeError, match="IVF_FLAT and L2"):
        await index.initialize()
    client.create_index.assert_not_called()
    client.drop_index.assert_not_called()


@pytest.mark.parametrize("method", ["has_collection", "create_collection", "describe_collection",
                                    "list_indexes", "describe_index", "create_index", "load_collection"])
async def test_initialize_failure_is_retryable(setup_index, method):
    index, client, _, _ = setup_index
    if method == "create_collection":
        client.has_collection.return_value = False
    if method == "create_index":
        client.list_indexes.return_value = []
    error = RuntimeError("database unavailable")
    getattr(client, method).side_effect = error
    with pytest.raises(RuntimeError) as caught:
        await index.initialize()
    assert caught.value is error
    assert not index._initialized
    getattr(client, method).side_effect = None
    await index.initialize()
    assert index._initialized


async def test_user_isolation_upsert_update_search_and_idempotent_delete(setup_index):
    index, client, embeddings, rows = setup_index
    await index.initialize()
    await index.upsert("alice", "id-1", "original")
    await index.upsert("bob", "id-2", "other")
    await index.upsert("alice", "id-1", "updated")
    assert len(rows) == 2
    assert rows["id-1"]["content"] == "updated"
    with pytest.raises(ValueError, match="another user"):
        await index.upsert("bob", "id-1", "overwrite")
    assert embeddings.embed_documents.call_count == 3
    assert rows["id-1"]["user_id"] == "alice"
    assert await index.search("alice", "query") == [("id-1", 0.0)]
    assert await index.search("nobody", "query") == []
    await index.delete("bob", "id-1")
    assert "id-1" in rows
    await index.delete("alice", "id-1")
    await index.delete("alice", "id-1")
    assert set(rows) == {"id-2"}


async def test_filter_values_are_never_interpolated(setup_index):
    index, client, _, _ = setup_index
    await index.initialize()
    uid, mid = '" or user_id != "', '\\" or id != "'
    await index.upsert(uid, mid, "text")
    await index.search(uid, "query")
    assert client.search.call_args.kwargs["filter_params"] == {"uid": uid}
    await index.delete(uid, mid)
    assert client.delete.call_args.kwargs["filter_params"] == {"mid": mid, "uid": uid}


@pytest.mark.parametrize("operation", ["upsert", "search", "delete"])
async def test_operations_require_initialization(setup_index, operation):
    index, client, embeddings, _ = setup_index
    args = {"upsert": ("u", "m", "text"), "search": ("u", "query"), "delete": ("u", "m")}
    with pytest.raises(RuntimeError, match="initialize"):
        await getattr(index, operation)(*args[operation])
    client.query.assert_not_called()
    embeddings.embed_query.assert_not_called()


@pytest.mark.parametrize("field,value", [("user_id", ""), ("user_id", 1), ("user_id", "中" * 86),
                                        ("memory_id", None), ("memory_id", "x" * 101),
                                        ("content", "  "), ("content", "中" * 2667)])
async def test_upsert_validates_before_io(setup_index, field, value):
    index, client, embeddings, _ = setup_index
    args = {"user_id": "u", "memory_id": "m", "content": "text", field: value}
    with pytest.raises(ValueError):
        await index.upsert(**args)
    client.query.assert_not_called()
    embeddings.embed_documents.assert_not_called()


@pytest.mark.parametrize("kwargs", [
    {"user_id": " "}, {"user_id": "x" * 257}, {"query": None}, {"query": "x" * 8001},
    {"limit": True}, {"limit": 0}, {"limit": 101}, {"limit": 1.0},
    {"search_params": []}, {"search_params": {"metric_type": "IP"}},
    {"search_params": {"params": None}}, {"search_params": {"params": {"nprobe": False}}},
    {"search_params": {"params": {"nprobe": 0}}},
])
async def test_search_invalid_parameters(setup_index, kwargs):
    index, client, embeddings, _ = setup_index
    with pytest.raises(ValueError):
        await index.search(**({"user_id": "u", "query": "q"} | kwargs))
    embeddings.embed_query.assert_not_called()
    client.search.assert_not_called()


async def test_search_parameters_empty_response_and_numeric_result(setup_index):
    index, client, _, _ = setup_index
    await index.initialize()
    params = {"params": {"nprobe": 4}, "radius": 9.0}
    original = deepcopy(params)
    client.search.side_effect = None
    client.search.return_value = [[{"id": "memory", "distance": "1.25"}]]
    assert await index.search("u", "q", limit=2, search_params=params) == [("memory", 1.25)]
    assert params == original
    sent = client.search.call_args.kwargs
    assert sent["search_params"] == original | {"metric_type": "L2"}
    assert sent["limit"] == 2
    client.search.return_value = []
    assert await index.search("u", "q") == []
    client.search.return_value = [[]]
    assert await index.search("u", "q") == []


@pytest.mark.parametrize("vector", [[1], [1, float("nan"), 3], [1, float("inf"), 3],
                                    [1, True, 3], [1, "2", 3]])
async def test_reject_bad_embeddings_without_write_or_search(setup_index, vector):
    index, client, embeddings, _ = setup_index
    await index.initialize()
    embeddings.embed_documents.return_value = [vector]
    embeddings.embed_query.return_value = vector
    with pytest.raises(ValueError, match="embedding|Embedding"):
        await index.upsert("u", "m", "text")
    with pytest.raises(ValueError, match="embedding|Embedding"):
        await index.search("u", "q")
    client.upsert.assert_not_called()
    client.search.assert_not_called()


@pytest.mark.parametrize("vectors", [[], [[1, 2, 3], [1, 2, 3]]])
async def test_embedding_count_must_be_one(setup_index, vectors):
    index, client, embeddings, _ = setup_index
    await index.initialize()
    embeddings.embed_documents.return_value = vectors
    with pytest.raises(ValueError, match="exactly one"):
        await index.upsert("u", "m", "text")
    client.upsert.assert_not_called()


@pytest.mark.parametrize("method,operation,args", [
    ("query", "upsert", ("u", "m", "text")),
    ("upsert", "upsert", ("u", "m", "text")),
    ("search", "search", ("u", "q")), ("delete", "delete", ("u", "m")),
])
async def test_database_errors_propagate_and_do_not_poison_lock(setup_index, method, operation, args):
    index, client, _, _ = setup_index
    await index.initialize()
    old = getattr(client, method).side_effect
    error = RuntimeError("offline")
    getattr(client, method).side_effect = error
    with pytest.raises(RuntimeError) as caught:
        await getattr(index, operation)(*args)
    assert caught.value is error
    getattr(client, method).side_effect = old
    await getattr(index, operation)(*args)


async def test_embedding_failure_is_not_empty_search(setup_index):
    index, _, embeddings, _ = setup_index
    await index.initialize()
    embeddings.embed_query.side_effect = RuntimeError("embedding unavailable")
    with pytest.raises(RuntimeError, match="embedding unavailable"):
        await index.search("u", "q")


async def test_cross_instance_concurrent_first_write_keeps_owner(setup_index):
    index, client, embeddings, rows = setup_index
    other = MemoryVectorIndex(client, embeddings)
    await asyncio.gather(index.initialize(), other.initialize())
    results = await asyncio.gather(
        index.upsert("alice", "shared", "a"), other.upsert("bob", "shared", "b"),
        return_exceptions=True,
    )
    assert sum(isinstance(result, ValueError) for result in results) == 1
    assert len(rows) == 1
    client.upsert.assert_called_once()


async def test_cancellation_does_not_release_worker_lock_or_block_event_loop(setup_index):
    index, client, embeddings, rows = setup_index
    await index.initialize()
    started, proceed, finished = threading.Event(), threading.Event(), threading.Event()
    event_loop_thread = threading.get_ident()

    def embed(texts):
        assert threading.get_ident() != event_loop_thread
        started.set()
        assert proceed.wait(5)
        return [[1, 2, 3]]

    original = client.upsert.side_effect

    def upsert(**kwargs):
        try:
            return original(**kwargs)
        finally:
            finished.set()

    embeddings.embed_documents.side_effect = embed
    client.upsert.side_effect = upsert
    task = asyncio.create_task(index.upsert("alice", "shared", "a"))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        competing = asyncio.create_task(index.upsert("bob", "shared", "b"))
        await asyncio.sleep(0)
        proceed.set()
        with pytest.raises(ValueError, match="another user"):
            await competing
        assert rows["shared"]["user_id"] == "alice"
    finally:
        proceed.set()
        await asyncio.to_thread(finished.wait, 2)


@pytest.mark.parametrize("name", [None, "", "  ", "bad-name", "x" * 256, "biz"])
def test_collection_name_validation(setup_index, name):
    _, client, embeddings, _ = setup_index
    with pytest.raises(ValueError, match="collection|RAG"):
        MemoryVectorIndex(client, embeddings, name)


@pytest.mark.parametrize("args", [("", "m"), ("u", ""), ("u", "中" * 34), ("x" * 257, "m")])
async def test_delete_validation_before_io(setup_index, args):
    index, client, _, _ = setup_index
    with pytest.raises(ValueError):
        await index.delete(*args)
    client.delete.assert_not_called()


async def test_existing_nlist_bounds_search_probe(setup_index):
    index, client, embeddings, _ = setup_index
    client.describe_index.return_value["nlist"] = "8"
    await index.initialize()
    with pytest.raises(ValueError, match="nlist"):
        await index.search("u", "q", search_params={"params": {"nprobe": 16}})
    embeddings.embed_query.assert_not_called()
    await index.search("u", "q")
    assert client.search.call_args.kwargs["search_params"]["params"]["nprobe"] == 8
    await index.search("u", "q", search_params={"params": {"nprobe": 4}})


async def test_actual_sdk_forwards_parameterized_filters_without_network(setup_index):
    from types import SimpleNamespace
    from pymilvus.client.prepare import Prepare

    index, _, _, _ = setup_index
    await index.initialize()
    # Exercise the installed SDK methods; only the RPC connection is mocked.
    sdk = object.__new__(MilvusClient)
    sdk._db_name = "default"
    conn = Mock()
    sdk._get_connection = Mock(return_value=conn)
    conn.query.return_value = []
    conn.search.return_value = [[]]
    conn.delete.return_value = SimpleNamespace(primary_keys=[], delete_count=0, cost=0)
    index._client = sdk
    uid, mid = 'user"\\name', 'memory"\\id'
    await index.upsert(uid, mid, "content")
    assert conn.query.call_args.kwargs["expr_params"] == {"mid": mid}
    await index.search(uid, "query")
    assert conn.search.call_args.kwargs["expr_params"] == {"uid": uid}
    await index.delete(uid, mid)
    sent = conn.delete.call_args.kwargs
    assert sent["expr_params"] == {"mid": mid, "uid": uid}
    # Check actual protobuf expression-template construction as well.
    request = Prepare.delete_request(
        "agent_memories", sent["expression"], expr_params=sent["expr_params"],
        consistency_level="Strong",
    )
    assert request.expr_template_values["mid"].string_val == mid
    assert request.expr_template_values["uid"].string_val == uid
