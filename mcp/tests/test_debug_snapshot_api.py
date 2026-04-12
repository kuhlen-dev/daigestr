from unittest.mock import AsyncMock, MagicMock, patch

from conftest import load_server_module, run_async


def _load_api_server():
    class HTTPExceptionStub(Exception):
        def __init__(self, status_code: int, detail: str):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    fastapi_mock = MagicMock()
    app_mock = MagicMock()
    app_mock.post = lambda *a, **kw: (lambda f: f)
    app_mock.get = lambda *a, **kw: (lambda f: f)
    app_mock.delete = lambda *a, **kw: (lambda f: f)
    app_mock.put = lambda *a, **kw: (lambda f: f)
    app_mock.exception_handler = lambda *a, **kw: (lambda f: f)
    app_mock.include_router = MagicMock()
    fastapi_mock.FastAPI = MagicMock(return_value=app_mock)
    fastapi_mock.HTTPException = HTTPExceptionStub
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
    with patch.object(_server_api, "debug_snapshot_list", return_value=[{"id": 1}, {"id": 2}]) as list_mock:
        result = run_async(
            _server_api.api_list_debug_snapshots(
                request_id="req-1",
                job_id="job-1",
                stage="extract_result",
                limit=10,
            )
        )

    assert result["count"] == 2
    assert result["snapshots"][0]["id"] == 1
    assert list_mock.call_args.kwargs == {
        "request_id": "req-1",
        "job_id": "job-1",
        "stage": "extract_result",
        "limit": 10,
    }


def test_api_get_debug_snapshot_returns_payload():
    snapshot = {"id": 3, "payload_json": {"markdown": "# Test"}}

    with patch.object(_server_api, "debug_snapshot_get", return_value=snapshot):
        result = run_async(_server_api.api_get_debug_snapshot(3))

    assert result["id"] == 3
    assert result["payload_json"]["markdown"] == "# Test"


def test_api_get_debug_snapshot_raises_404_for_missing_snapshot():
    with patch.object(_server_api, "debug_snapshot_get", return_value=None):
        try:
            run_async(_server_api.api_get_debug_snapshot(999))
        except Exception as exc:
            assert getattr(exc, "status_code", None) == 404
            assert "999" in getattr(exc, "detail", str(exc))
        else:
            raise AssertionError("Expected HTTPException for missing snapshot")


def test_api_replay_debug_snapshot_normalize_returns_replayed_payload():
    snapshot = {"id": 7, "payload_json": {"extracted": {"foo": "bar"}, "meta": {"template_used": "invoice"}}}
    replayed = {"snapshot_id": 7, "template_name": "invoice", "normalized": {"amount": 12}}

    with patch.object(_server_api, "debug_snapshot_get", return_value=snapshot), \
         patch.object(_server_api, "replay_normalization_from_snapshot", new=AsyncMock(return_value=replayed)):
        result = run_async(_server_api.api_replay_debug_snapshot_normalize(7))

    assert result["snapshot_id"] == 7
    assert result["normalized"]["amount"] == 12


def test_api_replay_debug_snapshot_normalize_raises_404_for_missing_snapshot():
    with patch.object(_server_api, "debug_snapshot_get", return_value=None):
        try:
            run_async(_server_api.api_replay_debug_snapshot_normalize(999))
        except Exception as exc:
            assert getattr(exc, "status_code", None) == 404
            assert "999" in getattr(exc, "detail", str(exc))
        else:
            raise AssertionError("Expected HTTPException for missing snapshot")


def test_api_replay_debug_snapshot_normalize_maps_value_error_to_400():
    snapshot = {"id": 7, "payload_json": {"meta": {"template_used": "invoice"}}}

    with patch.object(_server_api, "debug_snapshot_get", return_value=snapshot), \
         patch.object(
             _server_api,
             "replay_normalization_from_snapshot",
             new=AsyncMock(side_effect=ValueError("Snapshot has no extracted payload to replay")),
         ):
        try:
            run_async(_server_api.api_replay_debug_snapshot_normalize(7))
        except Exception as exc:
            assert getattr(exc, "status_code", None) == 400
            assert "no extracted payload" in getattr(exc, "detail", str(exc))
        else:
            raise AssertionError("Expected HTTPException for invalid replay payload")
