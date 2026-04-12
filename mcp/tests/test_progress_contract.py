import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from conftest import PNG_100x100, load_server_module, run_async


_server = load_server_module(use_real_pil=False)


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


def _make_vision_response(content: str, tokens_total: int = 50) -> dict:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {
            "prompt_tokens": 20,
            "completion_tokens": 30,
            "total_tokens": tokens_total,
        },
    }


def test_convert_auto_job_progress_uses_canonical_shape():
    updates: list[dict] = []
    vision_resp = _make_vision_response("# Progress")

    def fake_job_update(job_id, status, progress_json=None):
        updates.append(
            {
                "job_id": job_id,
                "status": status,
                "progress": json.loads(progress_json) if progress_json else None,
            }
        )

    with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
         patch.object(_server, "call_mistral_vision_api", new=AsyncMock(return_value=vision_resp)), \
         patch.object(_server, "job_update", side_effect=fake_job_update):
        result = run_async(
            _server.convert_auto(
                file_data=PNG_100x100,
                filename="progress.png",
                source="progress.png",
                source_type="base64",
                input_meta={"_job_id": "job-123"},
                language="de",
                no_cache=True,
            )
        )

    assert result.success is True
    assert updates
    first = updates[0]
    assert first["job_id"] == "job-123"
    assert first["status"] == "processing"
    assert first["progress"]["status"] == "processing"
    assert first["progress"]["current_stage"] == "start"
    assert first["progress"]["message"] == "Starting conversion"
    assert first["progress"]["percent"] == 0
    assert first["progress"]["job_id"] == "job-123"
    assert first["progress"]["request_id"] == result.meta.request_id
    assert first["progress"]["attempt_mode"] == "default"
    assert first["progress"]["metadata"]["filename"] == "progress.png"


def test_api_get_job_normalizes_legacy_progress_payload():
    created_at = datetime.now(timezone.utc)
    updated_at = datetime.now(timezone.utc)

    with patch.object(
        _server_api,
        "job_get",
        return_value={
            "id": "job-legacy",
            "status": "processing",
            "created_at": created_at,
            "updated_at": updated_at,
            "progress_json": json.dumps({"step": "ocr", "detail": "page 1/5", "percent": 20}),
        },
    ):
        response = run_async(_server_api.api_get_job("job-legacy"))

    assert response.job_id == "job-legacy"
    assert response.progress is not None
    assert response.progress.current_stage == "ocr"
    assert response.progress.message == "page 1/5"
    assert response.progress.percent == 20


def test_api_list_jobs_materializes_null_progress_when_missing():
    now = datetime.now(timezone.utc)

    with patch.object(
        _server_api,
        "job_list",
        return_value=[
            {
                "id": "job-1",
                "status": "queued",
                "created_at": now,
                "updated_at": now,
                "progress_json": None,
            }
        ],
    ):
        response = run_async(_server_api.api_list_jobs())

    assert len(response.jobs) == 1
    assert response.jobs[0].job_id == "job-1"
    assert response.jobs[0].progress is None


def test_convert_auto_job_progress_includes_page_counters_for_pdf_page_description():
    updates: list[dict] = []

    async def fake_describe_page_images(page_images, language="de", progress_callback=None, **_kwargs):
        assert len(page_images) == 2
        progress_callback("page 1/2", 30)
        progress_callback("page 2/2", 40)
        return ["desc-1", "desc-2"]

    def fake_job_update(job_id, status, progress_json=None):
        updates.append(
            {
                "job_id": job_id,
                "status": status,
                "progress": json.loads(progress_json) if progress_json else None,
            }
        )

    with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
         patch.object(_server, "detect_mimetype_from_bytes", return_value="application/pdf"), \
         patch.object(_server, "get_mimetype", return_value="application/pdf"), \
         patch.object(_server, "convert_with_markitdown", return_value={"success": True, "markdown": "# Doc"}), \
         patch.object(_server, "render_pdf_pages_as_images", return_value=[b"page-1", b"page-2"]), \
         patch.object(_server, "describe_page_images", new=fake_describe_page_images), \
         patch.object(_server, "insert_page_descriptions", return_value="# Doc\n\n[pages described]"), \
         patch.object(_server, "job_update", side_effect=fake_job_update):
        result = run_async(
            _server.convert_auto(
                file_data=b"%PDF-1.4 test",
                filename="progress.pdf",
                source="progress.pdf",
                source_type="base64",
                input_meta={"_job_id": "job-pages"},
                describe_pages=True,
                language="de",
                no_cache=True,
            )
        )

    assert result.success is True
    describe_pages_update = next(u for u in updates if u["progress"]["current_stage"] == "describe_pages")
    assert describe_pages_update["progress"]["page_current"] == 0
    assert describe_pages_update["progress"]["page_total"] == 2

    describe_page_update = next(u for u in updates if u["progress"]["current_stage"] == "describe_page")
    assert describe_page_update["progress"]["page_current"] == 1
    assert describe_page_update["progress"]["page_total"] == 2
