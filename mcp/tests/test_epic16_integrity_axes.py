import sys
import uuid
from unittest.mock import MagicMock

from conftest import load_server_module, run_async


DATABASE_URL = "postgresql://daigestr:daigestr@localhost:15432/daigestr"


def _load_api_rest_module():
    server = load_server_module(isolate_runtime_state=False)
    return server._test_module_bindings["api_rest"]


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


def test_normalizer_drift_axis_is_visible_in_health(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    api_server = _load_api_server()
    monkeypatch.setattr(api_server, "check_persistence_health", lambda: {"ready": True, "database_url_configured": True, "connection_ok": True, "missing_tables": []})
    monkeypatch.setattr(
        api_server,
        "get_normalization_drift_summary",
        lambda limit=10: {
            "total_enabled_templates": 2,
            "mapped_templates": 1,
            "unmapped_templates": 1,
            "normalized_field_count": 55,
            "missing_template_ids": ["invoice-v2"],
            "missing_template_sample_count": 1,
            "mapping_drift_detected": True,
        },
    )

    response = run_async(api_server.api_health())

    assert response.meta["normalizer_drift"]["mapping_drift_detected"] is True
    assert response.meta["normalizer_drift"]["missing_template_ids"] == ["invoice-v2"]


def test_idempotency_axis_reuses_async_execution_and_batch(monkeypatch):
    from models import BatchCreateRequest, ConvertRequest

    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    api_rest = _load_api_rest_module()
    monkeypatch.setattr(api_rest, "QUEUE_ENABLED", True)
    monkeypatch.setattr(api_rest, "BATCH_DEFAULT_QUEUE_NAME", "default")

    server_module = sys.modules.get("server")
    if server_module is not None:
        monkeypatch.setitem(server_module.__dict__, "QUEUE_ENABLED", True)
        monkeypatch.setitem(server_module.__dict__, "BATCH_DEFAULT_QUEUE_NAME", "default")

    request_a = ConvertRequest(base64="aGVsbG8=", filename="same.txt", meta={"idempotency_key": "async-axis"})
    request_b = ConvertRequest(base64="aGVsbG8=", filename="same.txt", meta={"idempotency_key": "async-axis"})
    _, execution_id_a = api_rest._ensure_execution_for_request(request_a, execution_kind="async", job_id=f"job-{uuid.uuid4()}")
    _, execution_id_b = api_rest._ensure_execution_for_request(request_b, execution_kind="async", job_id=f"job-{uuid.uuid4()}")

    batch_body = BatchCreateRequest(
        batch_ref="axis-batch",
        idempotency_key="batch-axis",
        items=[ConvertRequest(base64="aGVsbG8=", filename="same.txt")],
    )
    batch_a = api_rest._start_batch_execution(batch_body)
    batch_b = api_rest._start_batch_execution(batch_body)

    assert execution_id_a == execution_id_b
    assert batch_a.batch_id == batch_b.batch_id


def test_retention_axis_clears_artifacts_before_payload_rows():
    from execution_db import execution_create, execution_get, execution_result_cleanup, execution_result_get_final, execution_result_upsert, init_execution_db

    init_execution_db()
    execution_id = f"exec-{uuid.uuid4()}"
    execution_create(
        execution_id=execution_id,
        request_id=f"req-{uuid.uuid4()}",
        execution_kind="direct",
        source_type="file",
        source_ref="/data/retention.txt",
        document_identity={"filename": "retention.txt"},
        policy_context={},
        status="completed",
        current_stage="done",
    )
    execution_result_upsert(
        result_id=f"result-{uuid.uuid4()}",
        execution_id=execution_id,
        is_final=True,
        result_status="completed",
        success=True,
        response_json={"success": True, "markdown": "# retention"},
        meta={"execution_id": execution_id},
        artifact_refs={"markdown_path": "/tmp/retention.md"},
    )

    cleanup_artifacts = execution_result_cleanup(artifact_retention_days=0)
    after_artifacts = execution_result_get_final(execution_id)
    cleanup_payloads = execution_result_cleanup(retention_days=0)
    after_payloads = execution_result_get_final(execution_id)

    assert cleanup_artifacts["cleared_artifacts"] >= 1
    assert after_artifacts is not None
    assert after_artifacts["artifact_refs"] is None
    assert cleanup_payloads["deleted_results"] >= 1
    assert after_payloads is None


def test_replay_axis_creates_new_execution_without_mutating_source(monkeypatch):
    import routing
    from debug_snapshot_db import debug_snapshot_store, init_debug_snapshot_db
    from execution_db import execution_get, execution_result_get_final, init_execution_db
    from models import create_success_response

    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    api_rest = _load_api_rest_module()
    init_execution_db()
    init_debug_snapshot_db()

    async def fake_impl(**kwargs):
        return create_success_response(
            "# replay source",
            meta={
                "quality_score": 0.9,
                "template_used": "invoice",
                "template_version": 1,
            },
        )

    monkeypatch.setattr(routing, "_convert_auto_impl", fake_impl)
    server_module = sys.modules.get("server")
    if server_module is not None:
        monkeypatch.setitem(server_module.__dict__, "_convert_auto_impl", fake_impl)

    source = run_async(
        routing.convert_auto(
            file_data=b"hello replay",
            filename="replay.txt",
            source="/tmp/replay.txt",
            source_type="file",
            input_meta={"_request_id": f"req-{uuid.uuid4()}"},
            mode="default",
            no_cache=True,
        )
    )
    source_execution_id = source.meta.execution_id
    debug_snapshot_store(
        request_id=source.meta.request_id,
        stage="normalize",
        payload={
            "markdown": "# replay snapshot",
            "normalized": {"invoice_number": "INV-REPLAY"},
            "meta": {"template_used": "invoice"},
        },
    )

    replay = api_rest._replay_execution(source_execution_id)
    source_execution = execution_get(source_execution_id)
    replay_execution = execution_get(replay.execution_id)
    replay_result = execution_result_get_final(replay.execution_id)

    assert replay.execution_id != source_execution_id
    assert source_execution["execution_kind"] == "direct"
    assert source_execution["status"] == "completed"
    assert replay_execution["execution_kind"] == "replay"
    assert replay_execution["policy_context"]["replay"]["source_execution_id"] == source_execution_id
    assert replay_result["response_json"]["normalized"]["invoice_number"] == "INV-REPLAY"
