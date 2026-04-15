import asyncio
import sys
import uuid

import pytest

from conftest import load_server_module, run_async


DATABASE_URL = "postgresql://daigestr:daigestr@localhost:15432/daigestr"


@pytest.fixture(autouse=True)
def _force_test_database(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)


def _load_api_rest_module():
    server = load_server_module(isolate_runtime_state=False)
    return server._test_module_bindings["api_rest"]


def _install_successful_convert(monkeypatch, *, markdown: str, template_used: str = "invoice", extracted=None, api_rest=None):
    from models import create_success_response
    import routing

    async def fake_convert_auto(**kwargs):
        response = create_success_response(
            markdown,
            meta={
                "quality_score": 0.93,
                "template_used": template_used,
                "template_version": 1,
            },
        )
        if extracted is not None:
            response.extracted = extracted
        return response

    monkeypatch.setattr(routing, "_convert_auto_impl", fake_convert_auto)
    if api_rest is not None:
        monkeypatch.setattr(api_rest, "convert_auto", routing.convert_auto)

    server_module = sys.modules.get("server")
    if server_module is not None:
        monkeypatch.setitem(server_module.__dict__, "_convert_auto_impl", fake_convert_auto)
        if api_rest is not None:
            monkeypatch.setitem(server_module.__dict__, "convert_auto", routing.convert_auto)

    return fake_convert_auto


def _install_queue_worker_exit(monkeypatch, api_rest):
    async def stop_sleep(_seconds):
        raise asyncio.CancelledError()

    monkeypatch.setattr(api_rest.asyncio, "sleep", stop_sleep)


def test_direct_execution_ring_exposes_canonical_status_and_result(monkeypatch):
    import routing
    from execution_db import init_execution_db

    api_rest = _load_api_rest_module()
    init_execution_db()
    _install_successful_convert(monkeypatch, markdown="# direct e2e", extracted={"invoice_number": "INV-16"}, api_rest=api_rest)

    request_id = f"req-{uuid.uuid4()}"
    response = run_async(
        routing.convert_auto(
            file_data=b"hello direct",
            filename="direct.txt",
            source="/tmp/direct.txt",
            source_type="file",
            input_meta={"_request_id": request_id},
            mode="default",
            no_cache=True,
        )
    )

    status_payload = api_rest._get_execution_status_payload(response.meta.execution_id)
    result_payload = api_rest._get_execution_result_payload(response.meta.execution_id)

    assert status_payload.execution_kind == "direct"
    assert status_payload.status == "completed"
    assert status_payload.final_result_available is True
    assert status_payload.result_meta_summary["template_used"] == "invoice"
    assert result_payload.success is True
    assert result_payload.markdown == "# direct e2e"
    assert result_payload.extracted["invoice_number"] == "INV-16"


def test_async_execution_ring_runs_through_queue_worker(monkeypatch):
    from execution_db import execution_queue_complete, execution_queue_get_by_execution_id, init_execution_db
    from models import ConvertRequest

    api_rest = _load_api_rest_module()
    init_execution_db()
    _install_successful_convert(monkeypatch, markdown="# async e2e", extracted={"kind": "async"}, api_rest=api_rest)
    monkeypatch.setattr(api_rest, "QUEUE_ENABLED", True)

    server_module = sys.modules.get("server")
    if server_module is not None:
        monkeypatch.setitem(server_module.__dict__, "QUEUE_ENABLED", True)

    request = ConvertRequest(base64="aGVsbG8=", filename="async.txt", meta={})
    queued = run_async(api_rest._start_async_execution(request))
    queue_row = execution_queue_get_by_execution_id(queued.execution_id)
    queued_request = ConvertRequest(**queue_row["payload"]["request"])

    run_async(api_rest._run_async_job(queued.job_id, queued_request))
    execution_queue_complete(queue_row["id"])

    queue_row = execution_queue_get_by_execution_id(queued.execution_id)
    status_payload = api_rest._get_execution_status_payload(queued.execution_id)
    result_payload = api_rest._get_execution_result_payload(queued.execution_id)

    assert queue_row is not None
    assert queue_row["status"] == "completed"
    assert status_payload.execution_kind == "async"
    assert status_payload.status == "completed"
    assert result_payload.markdown == "# async e2e"
    assert result_payload.extracted["kind"] == "async"


def test_batch_execution_ring_runs_item_to_canonical_result(monkeypatch):
    from execution_db import execution_queue_complete, execution_queue_get_by_execution_id, init_execution_db
    from models import BatchCreateRequest, ConvertRequest

    api_rest = _load_api_rest_module()
    init_execution_db()
    _install_successful_convert(monkeypatch, markdown="# batch e2e", extracted={"kind": "batch"}, api_rest=api_rest)
    monkeypatch.setattr(api_rest, "QUEUE_ENABLED", True)
    monkeypatch.setattr(api_rest, "BATCH_DEFAULT_QUEUE_NAME", "default")

    server_module = sys.modules.get("server")
    if server_module is not None:
        monkeypatch.setitem(server_module.__dict__, "QUEUE_ENABLED", True)
        monkeypatch.setitem(server_module.__dict__, "BATCH_DEFAULT_QUEUE_NAME", "default")

    created = api_rest._start_batch_execution(
        BatchCreateRequest(
            batch_ref=f"e2e-batch-{uuid.uuid4()}",
            items=[ConvertRequest(base64="aGVsbG8=", filename="batch.txt", meta={"document_id": 1601})],
        )
    )
    batch_item = api_rest._get_batch_items_payload(created.batch_id, limit=10, offset=0).items[0]
    queue_row = execution_queue_get_by_execution_id(batch_item.execution_id)
    queued_request = ConvertRequest(**queue_row["payload"]["request"])

    run_async(api_rest._run_async_job(queue_row["job_id"], queued_request))
    execution_queue_complete(queue_row["id"])

    queue_row = execution_queue_get_by_execution_id(batch_item.execution_id)
    refreshed_item = api_rest._require_batch_item_row(created.batch_id, batch_item.batch_item_id)
    batch_status = api_rest._get_batch_status_payload(created.batch_id)
    item_result = api_rest._get_batch_item_result_payload(created.batch_id, batch_item.batch_item_id)

    assert queue_row is not None
    assert queue_row["status"] == "completed"
    assert refreshed_item["effective_status"] == "completed"
    assert batch_status.completed_count == 1
    assert batch_status.status == "completed"
    assert item_result.markdown == "# batch e2e"
    assert item_result.extracted["kind"] == "batch"
