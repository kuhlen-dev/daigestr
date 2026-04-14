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
    assert len(execution_status.attempts) == 1

    execution_result = api_rest._get_execution_result_payload(response.meta.execution_id)
    assert execution_result.success is True
    assert execution_result.markdown == "# execution result"
    assert execution_result.meta.execution_id == response.meta.execution_id


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
