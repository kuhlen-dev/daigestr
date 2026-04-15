import asyncio
import sys
import uuid
from pathlib import Path

import pytest

from conftest import load_server_module, run_async


DATABASE_URL = "postgresql://daigestr:daigestr@localhost:15432/daigestr"


@pytest.fixture(autouse=True)
def _force_test_database(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)


def _load_api_rest_module():
    server = load_server_module(isolate_runtime_state=False)
    return server._test_module_bindings["api_rest"]


def test_convert_auto_persists_execution_attempts_and_final_result(monkeypatch):
    import routing
    from execution_db import execution_attempt_list, execution_get, execution_result_get_final, init_execution_db
    from models import create_success_response

    init_execution_db()
    request_id = f"req-{uuid.uuid4()}"

    async def fake_impl(**kwargs):
        return create_success_response(
            "# ok",
            meta={
                "quality_score": 0.88,
                "template_used": "invoice",
                "template_version": 1,
            },
        )

    monkeypatch.setattr(routing, "_convert_auto_impl", fake_impl)
    server_module = sys.modules.get("server")
    if server_module is not None:
        monkeypatch.setitem(server_module.__dict__, "_convert_auto_impl", fake_impl)

    response = run_async(
        routing.convert_auto(
            file_data=b"hello",
            filename="hello.txt",
            source="/tmp/hello.txt",
            source_type="file",
            input_meta={"_request_id": request_id},
            mode="default",
            no_cache=True,
        )
    )

    assert response.success is True
    assert response.meta.execution_id

    execution_row = execution_get(response.meta.execution_id)
    assert execution_row["request_id"] == request_id
    assert execution_row["status"] == "completed"
    assert execution_row["document_identity"]["filename"] == "hello.txt"
    assert execution_row["document_identity"]["source"] == "/tmp/hello.txt"
    assert execution_row["document_identity"]["source_type"] == "file"
    assert execution_row["document_identity"]["size_bytes"] == 5
    assert execution_row["document_identity"]["extension"] == "txt"
    assert execution_row["document_identity"]["sha256"]
    assert execution_row["input_snapshot"]["source_type"] == "file"
    assert execution_row["input_snapshot"]["resolved_path"] == "/tmp/hello.txt"
    assert execution_row["input_snapshot"]["filename"] == "hello.txt"
    assert execution_row["input_snapshot"]["document_identity"]["sha256"] == execution_row["document_identity"]["sha256"]

    attempts = execution_attempt_list(response.meta.execution_id)
    assert len(attempts) == 1
    assert attempts[0]["attempt_number"] == 1
    assert attempts[0]["attempt_mode"] == "default"
    assert attempts[0]["status"] == "completed"

    final_result = execution_result_get_final(response.meta.execution_id)
    assert final_result is not None
    assert final_result["success"] is True
    assert final_result["meta"]["execution_id"] == response.meta.execution_id
    assert final_result["meta"]["request_id"] == request_id


def test_ensure_execution_for_async_request_persists_execution():
    from execution_db import execution_get, init_execution_db
    from models import ConvertRequest

    api_rest = _load_api_rest_module()
    init_execution_db()

    request = ConvertRequest(
        path="/data/sample.txt",
        meta={},
    )

    job_id = str(uuid.uuid4())
    _, execution_id = api_rest._ensure_execution_for_request(request, execution_kind="async", job_id=job_id)

    execution_row = execution_get(execution_id)
    assert execution_row["job_id"] == job_id
    assert execution_row["status"] == "queued"
    assert execution_row["execution_kind"] == "async"


def test_async_timeout_marks_execution_failed(monkeypatch):
    from execution_db import execution_get, execution_result_get_final, init_execution_db
    from models import ConvertRequest
    from templates_db import job_create

    api_rest = _load_api_rest_module()
    init_execution_db()
    monkeypatch.setattr(api_rest, "JOB_TIMEOUT_SECONDS", 0.01)

    async def slow_convert_auto(**kwargs):
        await asyncio.sleep(0.05)
        raise AssertionError("should have timed out before completion")

    monkeypatch.setattr(api_rest, "convert_auto", slow_convert_auto)
    server_module = sys.modules.get("server")
    if server_module is not None:
        monkeypatch.setitem(server_module.__dict__, "JOB_TIMEOUT_SECONDS", 0.01)
        monkeypatch.setitem(server_module.__dict__, "convert_auto", slow_convert_auto)

    request = ConvertRequest(
        base64="aGVsbG8=",
        filename="hello.txt",
        meta={},
    )
    job_id = str(uuid.uuid4())
    _, execution_id = api_rest._ensure_execution_for_request(request, execution_kind="async", job_id=job_id)
    job_create(job_id)

    run_async(api_rest._run_async_job(job_id, request))

    execution_row = execution_get(execution_id)
    assert execution_row["status"] == "failed"
    assert execution_row["current_stage"] == "failed"

    final_result = execution_result_get_final(execution_id)
    assert final_result is not None
    assert final_result["success"] is False
    assert final_result["error"]["code"] == "TIMEOUT"


def test_api_get_execution_and_result(monkeypatch):
    import routing
    from execution_db import init_execution_db
    from models import create_success_response

    api_rest = _load_api_rest_module()
    init_execution_db()
    request_id = f"req-{uuid.uuid4()}"

    async def fake_impl(**kwargs):
        return create_success_response(
            "# execution result",
            meta={
                "quality_score": 0.91,
                "template_used": "invoice",
                "template_version": 1,
            },
        )

    monkeypatch.setattr(routing, "_convert_auto_impl", fake_impl)
    server_module = sys.modules.get("server")
    if server_module is not None:
        monkeypatch.setitem(server_module.__dict__, "_convert_auto_impl", fake_impl)

    response = run_async(
        routing.convert_auto(
            file_data=b"hello",
            filename="hello.txt",
            source="/tmp/hello.txt",
            source_type="file",
            input_meta={"_request_id": request_id},
            mode="default",
            no_cache=True,
        )
    )

    execution_status = api_rest._get_execution_status_payload(response.meta.execution_id)
    assert execution_status.execution_id == response.meta.execution_id
    assert execution_status.request_id == request_id
    assert execution_status.final_result_available is True
    assert execution_status.result_meta_summary is not None
    assert execution_status.result_meta_summary["template_used"] == "invoice"
    assert execution_status.result_meta_summary["quality_score"] == 0.91
    assert len(execution_status.attempts) == 1

    execution_result = api_rest._get_execution_result_payload(response.meta.execution_id)
    assert execution_result.success is True
    assert execution_result.markdown == "# execution result"
    assert execution_result.meta.execution_id == response.meta.execution_id


def test_build_audit_result_meta_summary_contains_canonical_fields():
    import routing
    from models import create_success_response

    response = create_success_response(
        "# audited",
        meta={
            "document_type": "invoice",
            "document_type_confidence": 0.94,
            "template_used": "invoice",
            "template_version": 1,
            "quality_score": 0.89,
            "quality_grade": "good",
            "retry_applied": True,
            "retry_reason": "low_quality",
            "initial_mode": "default",
            "final_mode": "full",
            "initial_quality_score": 0.41,
            "final_quality_score": 0.89,
        },
    )
    metadata = routing._build_audit_result_meta_summary(response)
    assert metadata["document_type"] == "invoice"
    assert metadata["template_used"] == "invoice"
    assert metadata["quality_score"] == 0.89
    assert metadata["retry_applied"] is True
    assert metadata["initial_mode"] == "default"
    assert metadata["final_mode"] == "full"


def test_convert_auto_emits_final_audit_response_metadata_after_retry(monkeypatch):
    import routing
    from execution_db import init_execution_db
    from models import create_success_response

    init_execution_db()
    request_id = f"req-{uuid.uuid4()}"
    audit_events = []

    async def fake_impl(**kwargs):
        current_mode = kwargs["mode"]
        quality_score = 0.42 if current_mode == "default" else 0.87
        response = create_success_response(
            "# audited retry",
            meta={
                "quality_score": quality_score,
                "template_used": "invoice",
                "template_version": 1,
            },
        )
        response.extracted = {"invoice_number": "INV-1"}
        return response

    def fake_audit_log_event(*args, **kwargs):
        audit_events.append(kwargs)

    monkeypatch.setattr(routing, "_convert_auto_impl", fake_impl)
    monkeypatch.setattr(routing, "_audit_log_event", fake_audit_log_event)
    server_module = sys.modules.get("server")
    if server_module is not None:
        monkeypatch.setitem(server_module.__dict__, "_convert_auto_impl", fake_impl)

    response = run_async(
        routing.convert_auto(
            file_data=b"hello",
            filename="hello.txt",
            source="/tmp/hello.txt",
            source_type="file",
            input_meta={"_request_id": request_id},
            mode="default",
            template="invoice",
            retry_on_low_quality=True,
            quality_retry_threshold=0.75,
            quality_retry_mode="full",
            no_cache=True,
        )
    )

    assert response.success is True
    response_events = [event for event in audit_events if event.get("event_type") == "response"]
    assert len(response_events) == 1
    metadata = response_events[0]["metadata"]
    assert metadata["retry_applied"] is True
    assert metadata["initial_mode"] == "default"
    assert metadata["final_mode"] == "full"
    assert metadata["initial_quality_score"] == 0.42
    assert metadata["final_quality_score"] == 0.87


def test_direct_execution_status_includes_canonical_progress(monkeypatch):
    import routing
    from execution_db import init_execution_db
    from models import create_success_response

    api_rest = _load_api_rest_module()
    init_execution_db()
    request_id = f"req-{uuid.uuid4()}"

    async def fake_impl(**kwargs):
        return create_success_response(
            "# direct progress",
            meta={
                "quality_score": 0.73,
                "template_used": "invoice",
                "template_version": 1,
            },
        )

    monkeypatch.setattr(routing, "_convert_auto_impl", fake_impl)
    server_module = sys.modules.get("server")
    if server_module is not None:
        monkeypatch.setitem(server_module.__dict__, "_convert_auto_impl", fake_impl)

    response = run_async(
        routing.convert_auto(
            file_data=b"hello",
            filename="hello.txt",
            source="/tmp/hello.txt",
            source_type="file",
            input_meta={"_request_id": request_id},
            mode="default",
            no_cache=True,
        )
    )

    execution_status = api_rest._get_execution_status_payload(response.meta.execution_id)
    assert execution_status.status == "completed"
    assert execution_status.current_stage == "done"
    assert execution_status.progress is not None
    assert execution_status.progress.status == "completed"
    assert execution_status.progress.current_stage == "done"
    assert execution_status.progress.percent == 100
    assert execution_status.progress.request_id == request_id
    assert execution_status.progress.attempt_mode == "default"


def test_api_execution_diagnostics_returns_active_stuck_and_drift(monkeypatch):
    api_rest = _load_api_rest_module()
    server_module = sys.modules.get("server")

    active_row = {
        "id": "exec-active",
        "request_id": "req-active",
        "execution_kind": "direct",
        "source_type": "file",
        "source_ref": "/data/a.txt",
        "job_id": None,
        "batch_id": None,
        "batch_item_id": None,
        "status": "processing",
        "current_stage": "extract",
        "progress_json": {"status": "processing", "current_stage": "extract", "percent": 80},
        "document_identity": None,
        "policy_context": None,
        "warning_summary": None,
        "error_summary": None,
        "created_at": "2026-04-14T00:00:00Z",
        "updated_at": "2026-04-14T00:01:00Z",
        "started_at": "2026-04-14T00:00:01Z",
        "finished_at": None,
        "attempts": [],
        "final_result": None,
    }
    stuck_row = {
        **active_row,
        "id": "exec-stuck",
        "request_id": "req-stuck",
        "status": "processing",
        "current_stage": "ocr",
    }

    monkeypatch.setattr(api_rest, "execution_list_active", lambda limit=25: [{"id": "exec-active"}])
    monkeypatch.setattr(api_rest, "execution_list_stuck", lambda stuck_after_seconds, limit=25: [{"id": "exec-stuck"}])
    monkeypatch.setattr(
        api_rest,
        "execution_get_full",
        lambda execution_id: active_row if execution_id == "exec-active" else stuck_row,
    )
    monkeypatch.setattr(
        api_rest,
        "get_normalization_drift_summary",
        lambda limit=20: {
            "total_enabled_templates": 10,
            "mapped_templates": 9,
            "unmapped_templates": 1,
            "missing_template_ids": ["telecom_bill"],
            "mapping_drift_detected": True,
        },
    )
    if server_module is not None:
        monkeypatch.setitem(server_module.__dict__, "execution_list_active", lambda limit=25: [{"id": "exec-active"}])
        monkeypatch.setitem(server_module.__dict__, "execution_list_stuck", lambda stuck_after_seconds, limit=25: [{"id": "exec-stuck"}])
        monkeypatch.setitem(
            server_module.__dict__,
            "execution_get_full",
            lambda execution_id: active_row if execution_id == "exec-active" else stuck_row,
        )
        monkeypatch.setitem(
            server_module.__dict__,
            "get_normalization_drift_summary",
            lambda limit=20: {
                "total_enabled_templates": 10,
                "mapped_templates": 9,
                "unmapped_templates": 1,
                "missing_template_ids": ["telecom_bill"],
                "mapping_drift_detected": True,
            },
        )

    result = api_rest._get_execution_diagnostics_payload(limit=5, stuck_after_seconds=600, drift_sample_limit=3)

    assert result.active_count == 1
    assert result.stuck_count == 1
    assert result.stuck_threshold_seconds == 600
    assert result.active_executions[0].execution_id == "exec-active"
    assert result.stuck_executions[0].execution_id == "exec-stuck"
    assert result.normalizer_drift["mapping_drift_detected"] is True
    assert result.normalizer_drift["missing_template_ids"] == ["telecom_bill"]


def test_ensure_execution_for_request_reuses_explicit_idempotency_key_for_direct():
    from execution_db import init_execution_db
    from models import ConvertRequest

    api_rest = _load_api_rest_module()
    init_execution_db()

    request_a = ConvertRequest(path="/data/sample.txt", meta={"idempotency_key": "same-doc"})
    request_b = ConvertRequest(path="/data/sample.txt", meta={"idempotency_key": "same-doc"})

    _, execution_a = api_rest._ensure_execution_for_request(request_a, execution_kind="direct")
    _, execution_b = api_rest._ensure_execution_for_request(request_b, execution_kind="direct")

    assert execution_a == execution_b
    assert request_a.meta["idempotency_key"] == "same-doc"
    assert request_b.meta["idempotency_key"] == "same-doc"


def test_ensure_execution_for_request_reuses_explicit_idempotency_key_for_async():
    from execution_db import init_execution_db
    from models import ConvertRequest

    api_rest = _load_api_rest_module()
    init_execution_db()

    request_a = ConvertRequest(base64="aGVsbG8=", filename="hello.txt", meta={"idempotency_key": "async-doc"})
    request_b = ConvertRequest(base64="aGVsbG8=", filename="hello.txt", meta={"idempotency_key": "async-doc"})

    _, execution_a = api_rest._ensure_execution_for_request(request_a, execution_kind="async", job_id="job-a")
    _, execution_b = api_rest._ensure_execution_for_request(request_b, execution_kind="async", job_id="job-b")

    assert execution_a == execution_b
    assert request_a.meta["idempotency_key"] == "async-doc"
    assert request_b.meta["idempotency_key"] == "async-doc"


def test_execution_status_response_includes_input_snapshot():
    import uuid
    from execution_db import execution_create, execution_get_full, init_execution_db

    api_rest = _load_api_rest_module()
    init_execution_db()
    execution = execution_create(
        execution_id=str(uuid.uuid4()),
        request_id=str(uuid.uuid4()),
        execution_kind="direct",
        source_type="file",
        source_ref="/data/doc.txt",
        document_identity={"filename": "doc.txt"},
        input_snapshot={"source_type": "file", "resolved_path": "/data/doc.txt", "filename": "doc.txt"},
        status="completed",
        current_stage="done",
    )
    payload = api_rest._build_execution_status_response(execution_get_full(execution["id"]))

    assert payload.input_snapshot["resolved_path"] == "/data/doc.txt"


def test_async_execution_status_matches_job_progress(monkeypatch):
    import routing
    from execution_db import init_execution_db
    from models import ConvertRequest, create_success_response
    from templates_db import job_create, job_get

    api_rest = _load_api_rest_module()
    init_execution_db()

    async def fake_impl(**kwargs):
        return create_success_response(
            "# async progress",
            meta={
                "quality_score": 0.81,
                "template_used": "invoice",
                "template_version": 1,
            },
        )

    monkeypatch.setattr(routing, "_convert_auto_impl", fake_impl)
    monkeypatch.setattr(api_rest, "convert_auto", routing.convert_auto)
    server_module = sys.modules.get("server")
    if server_module is not None:
        monkeypatch.setitem(server_module.__dict__, "_convert_auto_impl", fake_impl)
        monkeypatch.setitem(server_module.__dict__, "convert_auto", routing.convert_auto)

    request = ConvertRequest(
        base64="aGVsbG8=",
        filename="hello.txt",
        meta={},
    )
    job_id = str(uuid.uuid4())
    _, execution_id = api_rest._ensure_execution_for_request(request, execution_kind="async", job_id=job_id)
    job_create(job_id)

    run_async(api_rest._run_async_job(job_id, request))

    execution_status = api_rest._get_execution_status_payload(execution_id)
    job_row = job_get(job_id)

    assert job_row["status"] == "completed"
    assert execution_status.status == "completed"
    assert execution_status.progress is not None
    assert execution_status.progress.status == "completed"
    assert execution_status.progress.current_stage == "done"
    assert execution_status.progress.request_id is not None
    assert execution_status.progress.attempt_mode == "default"


def test_api_convert_async_enqueues_when_queue_enabled(monkeypatch):
    from execution_db import execution_queue_list, init_execution_db
    from models import ConvertRequest

    api_rest = _load_api_rest_module()
    init_execution_db()
    captured = {"scheduled": False}

    def fail_if_scheduled(_coro):
        captured["scheduled"] = True
        raise AssertionError("asyncio.create_task must not be used when QUEUE_ENABLED=true")

    monkeypatch.setattr(api_rest.asyncio, "create_task", fail_if_scheduled)
    monkeypatch.setattr(api_rest, "QUEUE_ENABLED", True)
    server_module = sys.modules.get("server")
    if server_module is not None:
        monkeypatch.setitem(server_module.__dict__, "QUEUE_ENABLED", True)

    response = run_async(api_rest._start_async_execution(ConvertRequest(base64="aGVsbG8=", filename="hello.txt")))

    assert response.status == "queued"
    assert captured["scheduled"] is False
    queue_rows = execution_queue_list(limit=20)
    assert any(row["job_id"] == response.job_id and row["execution_id"] == response.execution_id for row in queue_rows)


def test_start_batch_execution_persists_batch_items_and_linked_executions(monkeypatch):
    from execution_db import (
        execution_batch_get,
        execution_batch_item_list,
        execution_get_full,
        execution_queue_list,
        init_execution_db,
    )
    from models import BatchCreateRequest, ConvertRequest

    api_rest = _load_api_rest_module()
    init_execution_db()
    monkeypatch.setattr(api_rest, "QUEUE_ENABLED", True)
    monkeypatch.setattr(api_rest, "BATCH_DEFAULT_QUEUE_NAME", "default")
    monkeypatch.setattr(api_rest, "resolve_path", lambda path: Path(path))
    server_module = sys.modules.get("server")
    if server_module is not None:
        monkeypatch.setitem(server_module.__dict__, "QUEUE_ENABLED", True)
        monkeypatch.setitem(server_module.__dict__, "BATCH_DEFAULT_QUEUE_NAME", "default")

    body = BatchCreateRequest(
        batch_ref="family-import",
        idempotency_key=f"batch-{uuid.uuid4()}",
        meta={"source": "brix"},
        items=[
            ConvertRequest(path="/root/docker/daigestr/data/e16/t1633_invoice.txt", meta={"document_id": 1001}),
            ConvertRequest(base64="aGVsbG8=", filename="hello.txt", meta={"document_id": 1002}),
        ],
    )

    response = api_rest._start_batch_execution(body)

    assert response.status == "queued"
    assert response.item_count == 2
    batch_row = execution_batch_get(response.batch_id)
    assert batch_row is not None
    assert batch_row["batch_ref"] == "family-import"
    assert batch_row["metadata"]["source"] == "brix"

    items = execution_batch_item_list(response.batch_id)
    assert len(items) == 2
    assert items[0]["item_index"] == 0
    assert items[0]["execution_id"] is not None
    assert items[0]["metadata"]["batch_id"] == response.batch_id
    assert items[1]["metadata"]["document_id"] == 1002

    execution = execution_get_full(items[0]["execution_id"])
    assert execution is not None
    assert execution["execution_kind"] == "batch_item"
    assert execution["batch_id"] == response.batch_id
    assert execution["batch_item_id"] == items[0]["id"]
    assert execution["input_snapshot"]["document_identity"]["sha256"]

    queue_rows = execution_queue_list(limit=20)
    assert sum(1 for row in queue_rows if row["execution_id"] in {item["execution_id"] for item in items}) == 2


def test_start_batch_execution_reuses_batch_idempotency_key(monkeypatch):
    from models import BatchCreateRequest, ConvertRequest

    api_rest = _load_api_rest_module()
    monkeypatch.setattr(api_rest, "QUEUE_ENABLED", True)
    monkeypatch.setattr(api_rest, "BATCH_DEFAULT_QUEUE_NAME", "default")
    server_module = sys.modules.get("server")
    if server_module is not None:
        monkeypatch.setitem(server_module.__dict__, "QUEUE_ENABLED", True)
        monkeypatch.setitem(server_module.__dict__, "BATCH_DEFAULT_QUEUE_NAME", "default")

    body = BatchCreateRequest(
        batch_ref="same-batch",
        idempotency_key="batch-idem-1",
        items=[ConvertRequest(base64="aGVsbG8=", filename="hello.txt")],
    )

    first = api_rest._start_batch_execution(body)
    second = api_rest._start_batch_execution(body)

    assert first.batch_id == second.batch_id
    assert first.item_count == second.item_count == 1


def test_get_batch_status_aggregates_execution_counts(monkeypatch):
    from execution_db import execution_batch_status_summary, execution_update
    from models import BatchCreateRequest, ConvertRequest

    api_rest = _load_api_rest_module()
    monkeypatch.setattr(api_rest, "_get", lambda name, default: getattr(api_rest, name, default))
    monkeypatch.setattr(api_rest, "QUEUE_ENABLED", True)
    monkeypatch.setattr(api_rest, "BATCH_DEFAULT_QUEUE_NAME", "default")
    monkeypatch.setattr(api_rest, "execution_batch_status_summary", execution_batch_status_summary)
    server_module = sys.modules.get("server")
    if server_module is not None:
        monkeypatch.setitem(server_module.__dict__, "QUEUE_ENABLED", True)
        monkeypatch.setitem(server_module.__dict__, "BATCH_DEFAULT_QUEUE_NAME", "default")
        monkeypatch.setitem(server_module.__dict__, "execution_batch_status_summary", execution_batch_status_summary)

    response = api_rest._start_batch_execution(
        BatchCreateRequest(
            batch_ref="status-batch",
            idempotency_key=f"batch-status-{uuid.uuid4()}",
            items=[
                ConvertRequest(base64="aGVsbG8=", filename="hello.txt"),
                ConvertRequest(base64="d29ybGQ=", filename="world.txt"),
            ],
        )
    )

    batch = api_rest._get_batch_status_payload(response.batch_id)
    assert batch.status == "queued"
    assert batch.item_count == 2
    assert batch.queued_count == 2
    assert len(batch.active_items) == 2

    first_execution_id = batch.active_items[0].execution_id
    execution_update(first_execution_id, status="completed", current_stage="done", finished_at_now=True)

    refreshed = api_rest._get_batch_status_payload(response.batch_id)
    assert refreshed.completed_count == 1
    assert refreshed.queued_count == 1
    assert refreshed.status == "processing"


def test_list_batches_returns_lightweight_status_entries(monkeypatch):
    from execution_db import execution_batch_list, execution_batch_status_summary
    from models import BatchCreateRequest, ConvertRequest

    api_rest = _load_api_rest_module()
    monkeypatch.setattr(api_rest, "_get", lambda name, default: getattr(api_rest, name, default))
    monkeypatch.setattr(api_rest, "QUEUE_ENABLED", True)
    monkeypatch.setattr(api_rest, "BATCH_DEFAULT_QUEUE_NAME", "default")
    monkeypatch.setattr(api_rest, "execution_batch_list", execution_batch_list)
    monkeypatch.setattr(api_rest, "execution_batch_status_summary", execution_batch_status_summary)
    server_module = sys.modules.get("server")
    if server_module is not None:
        monkeypatch.setitem(server_module.__dict__, "QUEUE_ENABLED", True)
        monkeypatch.setitem(server_module.__dict__, "BATCH_DEFAULT_QUEUE_NAME", "default")
        monkeypatch.setitem(server_module.__dict__, "execution_batch_list", execution_batch_list)
        monkeypatch.setitem(server_module.__dict__, "execution_batch_status_summary", execution_batch_status_summary)

    created = api_rest._start_batch_execution(
        BatchCreateRequest(
            batch_ref=f"list-batch-{uuid.uuid4()}",
            items=[ConvertRequest(base64="aGVsbG8=", filename="hello.txt")],
        )
    )

    rows = api_rest.execution_batch_list(limit=10)
    batches = [
        api_rest._build_batch_status_response(
            api_rest.execution_batch_status_summary(row["id"], active_item_limit=api_rest.BATCH_STATUS_ACTIVE_ITEM_LIMIT)
        )
        for row in rows
    ]
    assert any(batch.batch_id == created.batch_id for batch in batches)


def test_list_batch_items_returns_paginated_execution_linked_entries(monkeypatch):
    from execution_db import execution_batch_item_list_paginated
    from models import BatchCreateRequest, ConvertRequest

    api_rest = _load_api_rest_module()
    monkeypatch.setattr(api_rest, "_get", lambda name, default: getattr(api_rest, name, default))
    monkeypatch.setattr(api_rest, "QUEUE_ENABLED", True)
    monkeypatch.setattr(api_rest, "BATCH_DEFAULT_QUEUE_NAME", "default")
    monkeypatch.setattr(api_rest, "execution_batch_item_list_paginated", execution_batch_item_list_paginated)
    server_module = sys.modules.get("server")
    if server_module is not None:
        monkeypatch.setitem(server_module.__dict__, "QUEUE_ENABLED", True)
        monkeypatch.setitem(server_module.__dict__, "BATCH_DEFAULT_QUEUE_NAME", "default")
        monkeypatch.setitem(server_module.__dict__, "execution_batch_item_list_paginated", execution_batch_item_list_paginated)

    created = api_rest._start_batch_execution(
        BatchCreateRequest(
            batch_ref=f"items-batch-{uuid.uuid4()}",
            items=[
                ConvertRequest(base64="aGVsbG8=", filename="hello.txt", meta={"document_id": 1}),
                ConvertRequest(base64="d29ybGQ=", filename="world.txt", meta={"document_id": 2}),
            ],
        )
    )

    page = api_rest._get_batch_items_payload(created.batch_id, limit=1, offset=0)
    assert page.batch_id == created.batch_id
    assert page.limit == 1
    assert page.offset == 0
    assert page.total_count == 2
    assert len(page.items) == 1
    assert page.items[0].execution_id is not None
    assert page.items[0].metadata["document_id"] == 1
    assert page.items[0].final_result_available is False


def test_get_batch_item_result_reuses_execution_result_payload(monkeypatch):
    from execution_db import execution_result_get_final, execution_result_upsert, execution_update
    from models import BatchCreateRequest, ConvertRequest, create_success_response

    api_rest = _load_api_rest_module()
    monkeypatch.setattr(api_rest, "QUEUE_ENABLED", True)
    monkeypatch.setattr(api_rest, "BATCH_DEFAULT_QUEUE_NAME", "default")
    server_module = sys.modules.get("server")
    if server_module is not None:
        monkeypatch.setitem(server_module.__dict__, "QUEUE_ENABLED", True)
        monkeypatch.setitem(server_module.__dict__, "BATCH_DEFAULT_QUEUE_NAME", "default")

    created = api_rest._start_batch_execution(
        BatchCreateRequest(
            batch_ref=f"item-result-batch-{uuid.uuid4()}",
            items=[ConvertRequest(base64="aGVsbG8=", filename="hello.txt")],
        )
    )
    batch_page = api_rest._get_batch_items_payload(created.batch_id, limit=10, offset=0)
    batch_item = batch_page.items[0]
    result = create_success_response(
        "# batch item result",
        meta={
            "execution_id": batch_item.execution_id,
            "quality_score": 0.93,
            "template_used": "invoice",
            "template_version": 1,
        },
    )
    result.extracted = {"invoice_number": "INV-1"}
    execution_result_upsert(
        result_id=f"result-{uuid.uuid4()}",
        execution_id=batch_item.execution_id,
        is_final=True,
        result_status="completed",
        success=True,
        response_json=result.model_dump(mode="json"),
        meta=result.meta.model_dump(mode="json"),
        extracted=result.extracted,
        normalized=result.normalized,
        warnings=[warning.model_dump(mode="json") for warning in result.warnings] if result.warnings else None,
        error=result.error.model_dump(mode="json") if result.error else None,
    )
    execution_update(batch_item.execution_id, status="completed", current_stage="done", finished_at_now=True)

    stored = execution_result_get_final(batch_item.execution_id)
    assert stored is not None

    restored = api_rest._get_batch_item_result_payload(created.batch_id, batch_item.batch_item_id)
    assert restored.success is True
    assert restored.markdown == "# batch item result"
    assert restored.meta.execution_id == batch_item.execution_id


def test_batch_item_cancel_resume_and_retry_orchestration(monkeypatch):
    from execution_db import execution_queue_get_by_execution_id, execution_result_get_final, execution_result_upsert, execution_update
    from fastapi import HTTPException
    from models import BatchCreateRequest, ConvertRequest, create_error_response, ErrorCode

    api_rest = _load_api_rest_module()
    monkeypatch.setattr(api_rest, "QUEUE_ENABLED", True)
    monkeypatch.setattr(api_rest, "BATCH_DEFAULT_QUEUE_NAME", "default")
    server_module = sys.modules.get("server")
    if server_module is not None:
        monkeypatch.setitem(server_module.__dict__, "QUEUE_ENABLED", True)
        monkeypatch.setitem(server_module.__dict__, "BATCH_DEFAULT_QUEUE_NAME", "default")

    created = api_rest._start_batch_execution(
        BatchCreateRequest(
            batch_ref=f"cancel-resume-batch-{uuid.uuid4()}",
            items=[ConvertRequest(base64="aGVsbG8=", filename="hello.txt")],
        )
    )
    batch_item = api_rest._get_batch_items_payload(created.batch_id, limit=10, offset=0).items[0]

    cancelled = api_rest._cancel_batch_item(created.batch_id, batch_item.batch_item_id)
    assert cancelled.status == "cancelled"

    cancelled_batch = api_rest._get_batch_status_payload(created.batch_id)
    assert cancelled_batch.cancelled_count == 1
    assert cancelled_batch.status == "cancelled"

    with pytest.raises(HTTPException):
        api_rest._retry_batch_item(created.batch_id, batch_item.batch_item_id)

    resumed = api_rest._resume_batch_item(created.batch_id, batch_item.batch_item_id)
    assert resumed.status == "queued"
    assert resumed.final_result_available is False

    resumed_queue = execution_queue_get_by_execution_id(batch_item.execution_id)
    assert resumed_queue is not None
    assert resumed_queue["status"] == "queued"

    failed_result = create_error_response(
        ErrorCode.CONVERSION_FAILED,
        "batch item failed",
        meta={"execution_id": batch_item.execution_id},
    )
    execution_result_upsert(
        result_id=f"result-{uuid.uuid4()}",
        execution_id=batch_item.execution_id,
        is_final=True,
        result_status="failed",
        success=False,
        response_json=failed_result.model_dump(mode="json"),
        meta=failed_result.meta.model_dump(mode="json"),
        extracted=None,
        normalized=None,
        warnings=None,
        error=failed_result.error.model_dump(mode="json") if failed_result.error else None,
    )
    execution_update(batch_item.execution_id, status="failed", current_stage="failed", finished_at_now=True)

    failed = api_rest._get_batch_items_payload(created.batch_id, limit=10, offset=0).items[0]
    assert failed.status == "failed"
    assert failed.final_result_available is True
    assert execution_result_get_final(batch_item.execution_id) is not None

    retried = api_rest._retry_batch_item(created.batch_id, batch_item.batch_item_id)
    assert retried.status == "queued"
    assert retried.final_result_available is False
    assert execution_result_get_final(batch_item.execution_id) is None


def test_batch_resume_requeues_only_cancelled_items(monkeypatch):
    from execution_db import execution_get, execution_queue_get_by_execution_id, execution_update
    from models import BatchCreateRequest, ConvertRequest

    api_rest = _load_api_rest_module()
    monkeypatch.setattr(api_rest, "QUEUE_ENABLED", True)
    monkeypatch.setattr(api_rest, "BATCH_DEFAULT_QUEUE_NAME", "default")
    server_module = sys.modules.get("server")
    if server_module is not None:
        monkeypatch.setitem(server_module.__dict__, "QUEUE_ENABLED", True)
        monkeypatch.setitem(server_module.__dict__, "BATCH_DEFAULT_QUEUE_NAME", "default")

    created = api_rest._start_batch_execution(
        BatchCreateRequest(
            batch_ref=f"resume-batch-{uuid.uuid4()}",
            items=[
                ConvertRequest(base64="aGVsbG8=", filename="hello.txt"),
                ConvertRequest(base64="d29ybGQ=", filename="world.txt"),
            ],
        )
    )
    items = api_rest._get_batch_items_payload(created.batch_id, limit=10, offset=0).items
    first, second = items

    api_rest._cancel_batch_item(created.batch_id, first.batch_item_id)
    execution_update(second.execution_id, status="completed", current_stage="done", finished_at_now=True)

    resumed_batch = api_rest._resume_batch(created.batch_id)
    assert resumed_batch.queued_count == 1
    assert resumed_batch.completed_count == 1
    assert resumed_batch.status == "processing"

    first_queue = execution_queue_get_by_execution_id(first.execution_id)
    assert first_queue is not None and first_queue["status"] == "queued"
    second_execution = execution_get(second.execution_id)
    assert second_execution is not None
    assert second_execution["status"] == "completed"


def test_queue_worker_skips_cancelled_batch_item_before_execution(monkeypatch):
    from execution_db import execution_queue_get_by_execution_id, execution_result_get_final
    from models import BatchCreateRequest, ConvertRequest

    api_rest = _load_api_rest_module()
    monkeypatch.setattr(api_rest, "QUEUE_ENABLED", True)
    monkeypatch.setattr(api_rest, "BATCH_DEFAULT_QUEUE_NAME", "default")
    server_module = sys.modules.get("server")
    if server_module is not None:
        monkeypatch.setitem(server_module.__dict__, "QUEUE_ENABLED", True)
        monkeypatch.setitem(server_module.__dict__, "BATCH_DEFAULT_QUEUE_NAME", "default")

    created = api_rest._start_batch_execution(
        BatchCreateRequest(
            batch_ref=f"worker-boundary-{uuid.uuid4()}",
            items=[ConvertRequest(base64="aGVsbG8=", filename="hello.txt")],
        )
    )
    batch_item = api_rest._get_batch_items_payload(created.batch_id, limit=10, offset=0).items[0]
    api_rest._cancel_batch_item(created.batch_id, batch_item.batch_item_id)
    queue_row = execution_queue_get_by_execution_id(batch_item.execution_id)
    assert queue_row is not None

    called = {"ran": False}

    async def fail_if_run(*args, **kwargs):
        called["ran"] = True
        raise AssertionError("_run_async_job must not run for cancelled executions")

    claim_rows = [queue_row, None]

    def fake_claim_next(**kwargs):
        return claim_rows.pop(0)

    async def stop_after_cancel(_seconds):
        raise asyncio.CancelledError()

    monkeypatch.setattr(api_rest, "_run_async_job", fail_if_run)
    monkeypatch.setattr(api_rest, "execution_queue_claim_next", fake_claim_next)
    monkeypatch.setattr(api_rest.asyncio, "sleep", stop_after_cancel)
    if server_module is not None:
        monkeypatch.setitem(server_module.__dict__, "execution_queue_claim_next", fake_claim_next)
        monkeypatch.setitem(server_module.__dict__, "_run_async_job", fail_if_run)

    with pytest.raises(asyncio.CancelledError):
        run_async(api_rest._queue_worker_loop("worker-boundary"))

    queue_row = execution_queue_get_by_execution_id(batch_item.execution_id)
    assert queue_row["status"] == "cancelled"
    assert called["ran"] is False
    assert execution_result_get_final(batch_item.execution_id) is None


def test_async_failed_convert_response_marks_job_failed(monkeypatch):
    import routing
    from execution_db import init_execution_db
    from models import ConvertRequest, create_error_response, ErrorCode
    from templates_db import job_create, job_get

    api_rest = _load_api_rest_module()
    init_execution_db()

    async def fake_impl(**kwargs):
        return create_error_response(
            ErrorCode.CONVERSION_FAILED,
            "structured extraction failed",
            meta={
                "template_used": "invoice",
                "template_version": 1,
            },
        )

    monkeypatch.setattr(routing, "_convert_auto_impl", fake_impl)
    monkeypatch.setattr(api_rest, "convert_auto", routing.convert_auto)
    server_module = sys.modules.get("server")
    if server_module is not None:
        monkeypatch.setitem(server_module.__dict__, "_convert_auto_impl", fake_impl)
        monkeypatch.setitem(server_module.__dict__, "convert_auto", routing.convert_auto)

    request = ConvertRequest(
        base64="aGVsbG8=",
        filename="hello.txt",
        meta={},
    )
    job_id = str(uuid.uuid4())
    _, execution_id = api_rest._ensure_execution_for_request(request, execution_kind="async", job_id=job_id)
    job_create(job_id)

    run_async(api_rest._run_async_job(job_id, request))

    job_row = job_get(job_id)
    execution_status = api_rest._get_execution_status_payload(execution_id)
    restored = api_rest._get_job_result_payload(job_id)

    assert job_row["status"] == "failed"
    assert execution_status.status == "failed"
    assert execution_status.progress is not None
    assert execution_status.progress.status == "failed"
    assert restored.success is False
    assert restored.error.code == ErrorCode.CONVERSION_FAILED


def test_api_get_job_result_falls_back_to_execution_result():
    from execution_db import execution_create, execution_result_upsert, init_execution_db
    from models import create_success_response
    from templates_db import job_create, job_update

    api_rest = _load_api_rest_module()
    init_execution_db()
    job_id = str(uuid.uuid4())
    execution_id = str(uuid.uuid4())
    request_id = f"req-{uuid.uuid4()}"

    job_create(job_id)
    job_update(job_id, "completed")
    execution_create(
        execution_id=execution_id,
        request_id=request_id,
        execution_kind="async",
        source_type="file",
        source_ref="/data/demo.pdf",
        job_id=job_id,
        status="completed",
        current_stage="completed",
    )
    response = create_success_response(
        "# from execution result",
        meta={
            "request_id": request_id,
            "execution_id": execution_id,
            "job_id": job_id,
            "quality_score": 0.84,
        },
    )
    execution_result_upsert(
        result_id=str(uuid.uuid4()),
        execution_id=execution_id,
        is_final=True,
        result_status="completed",
        success=True,
        response_json=response.model_dump(),
        meta=response.meta.model_dump(),
        extracted=response.extracted,
        normalized=response.normalized,
        artifact_refs={"has_markdown": True},
        warnings=response.normalized_warnings,
        error=None,
    )

    restored = api_rest._get_job_result_payload(job_id)
    assert restored.success is True
    assert restored.markdown == "# from execution result"
    assert restored.meta.execution_id == execution_id


def test_invalid_convert_input_does_not_create_execution():
    from execution_db import execution_list, init_execution_db
    from models import ConvertRequest

    api_rest = _load_api_rest_module()
    init_execution_db()
    before = execution_list(limit=500)

    request = ConvertRequest(
        path="/data/a.pdf",
        url="https://example.com/a.pdf",
        meta={},
    )

    response = run_async(api_rest._api_convert_impl(request))
    after = execution_list(limit=500)

    assert response.success is False
    assert response.error.code == "INVALID_INPUT"
    assert len(after) == len(before)
