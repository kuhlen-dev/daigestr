from unittest.mock import AsyncMock, MagicMock, patch

from conftest import load_server_module, run_async


def _load_api_server():
    fastapi_mock = MagicMock()
    app_mock = MagicMock()
    app_mock.post = lambda *a, **kw: (lambda f: f)
    app_mock.get = lambda *a, **kw: (lambda f: f)
    app_mock.delete = lambda *a, **kw: (lambda f: f)
    app_mock.put = lambda *a, **kw: (lambda f: f)
    app_mock.exception_handler = lambda *a, **kw: (lambda f: f)
    app_mock.include_router = MagicMock()
    fastapi_mock.FastAPI = MagicMock(return_value=app_mock)
    fastapi_mock.HTTPException = Exception
    fastapi_mock.Request = MagicMock()
    return load_server_module(
        use_real_pil=False,
        extra_patches={
            "fastapi": fastapi_mock,
            "fastapi.exceptions": MagicMock(),
            "fastapi.responses": MagicMock(),
        },
    )


_server_api = _load_api_server()


def test_api_list_debug_snapshots_uses_storage_filtering():
    with patch.object(_server_api, "debug_snapshot_list", return_value=[{"id": 1}, {"id": 2}]):
        result = run_async(_server_api.api_list_debug_snapshots(request_id="req-1", limit=10))

    assert result["count"] == 2
    assert result["snapshots"][0]["id"] == 1


def test_api_replay_debug_snapshot_normalize_returns_replayed_payload():
    snapshot = {"id": 7, "payload_json": {"extracted": {"foo": "bar"}, "meta": {"template_used": "invoice"}}}
    replayed = {"snapshot_id": 7, "template_name": "invoice", "normalized": {"amount": 12}}

    with patch.object(_server_api, "debug_snapshot_get", return_value=snapshot), \
         patch.object(_server_api, "replay_normalization_from_snapshot", new=AsyncMock(return_value=replayed)):
        result = run_async(_server_api.api_replay_debug_snapshot_normalize(7))

    assert result["snapshot_id"] == 7
    assert result["normalized"]["amount"] == 12
