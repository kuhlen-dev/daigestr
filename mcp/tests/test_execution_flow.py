import asyncio
import sys
import uuid

import pytest

from conftest import run_async


DATABASE_URL = "postgresql://daigestr:daigestr@localhost:15432/daigestr"


@pytest.fixture(autouse=True)
def _force_test_database(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)


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
    import api_rest
    from execution_db import execution_get, init_execution_db
    from models import ConvertRequest

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
    import api_rest
    from execution_db import execution_get, execution_result_get_final, init_execution_db
    from models import ConvertRequest
    from templates_db import job_create

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
