from unittest.mock import MagicMock

from conftest import load_server_module, run_async


def _load_server_with_audit_router():
    fastapi_mock = MagicMock()
    app_mock = MagicMock()
    app_mock.post = lambda *a, **kw: (lambda f: f)
    app_mock.get = lambda *a, **kw: (lambda f: f)
    app_mock.delete = lambda *a, **kw: (lambda f: f)
    app_mock.put = lambda *a, **kw: (lambda f: f)
    app_mock.exception_handler = lambda *a, **kw: (lambda f: f)
    app_mock.include_router = MagicMock()

    class _PassthroughRouter:
        def __init__(self, *args, **kwargs):
            pass

        def get(self, *args, **kwargs):
            return lambda f: f

        def post(self, *args, **kwargs):
            return lambda f: f

        def delete(self, *args, **kwargs):
            return lambda f: f

        def put(self, *args, **kwargs):
            return lambda f: f

    fastapi_mock.FastAPI = MagicMock(return_value=app_mock)
    fastapi_mock.APIRouter = _PassthroughRouter
    fastapi_mock.HTTPException = Exception
    fastapi_mock.Query = lambda default=None, **kwargs: default

    return load_server_module(
        use_real_pil=False,
        extra_patches={
            "fastapi": fastapi_mock,
            "fastapi.exceptions": MagicMock(),
            "fastapi.responses": MagicMock(),
        },
    )


_server = _load_server_with_audit_router()
import api_rest_audit as _audit_api
import routing as _routing
from models import ConvertResponse, MetaData


def test_build_audit_result_meta_summary_includes_warning_codes():
    response = ConvertResponse(
        success=True,
        markdown="# audited",
        warnings=[{"code": "used_retry", "message": "Retried"}, {"code": "normalizer_missing_mapping", "message": "No mapping"}],
        meta=MetaData(
            source="invoice.pdf",
            format="pdf",
            template_used="invoice",
            quality_score=0.91,
            retry_applied=True,
            retry_reason="low_quality",
        ),
    )

    metadata = _routing._build_audit_result_meta_summary(response)

    assert metadata["warning_count"] == 2
    assert metadata["warning_codes"] == ["used_retry", "normalizer_missing_mapping"]


def test_api_audit_get_by_request_returns_business_history_summary(monkeypatch):
    monkeypatch.setattr(_audit_api, "AUDIT_API_ENABLED", True)
    monkeypatch.setattr(
        _audit_api,
        "audit_get_by_request",
        lambda _request_id: [
            {
                "id": 1,
                "request_id": "req-1",
                "execution_id": "exec-1",
                "job_id": "job-1",
                "event_type": "request",
                "level": "info",
                "created_at": "2026-04-15T10:00:00Z",
                "metadata": None,
            },
            {
                "id": 2,
                "request_id": "req-1",
                "execution_id": "exec-1",
                "job_id": "job-1",
                "event_type": "response",
                "level": "warning",
                "created_at": "2026-04-15T10:00:05Z",
                "metadata": {
                    "success": True,
                    "document_type": "invoice",
                    "template_used": "invoice",
                    "template_version": 3,
                    "quality_score": 0.91,
                    "retry_applied": True,
                    "retry_reason": "low_quality",
                    "warning_count": 1,
                    "warning_codes": ["used_retry"],
                },
            },
        ],
    )

    payload = run_async(_audit_api.api_audit_get_by_request("req-1"))

    assert payload["history_summary"]["request_id"] == "req-1"
    assert payload["history_summary"]["execution_id"] == "exec-1"
    assert payload["history_summary"]["job_id"] == "job-1"
    assert payload["history_summary"]["document_type"] == "invoice"
    assert payload["history_summary"]["template_used"] == "invoice"
    assert payload["history_summary"]["warning_codes"] == ["used_retry"]
    assert payload["history_summary"]["warning_event_count"] == 1


def test_api_audit_list_returns_grouped_history_summaries(monkeypatch):
    monkeypatch.setattr(_audit_api, "AUDIT_API_ENABLED", True)
    monkeypatch.setattr(
        _audit_api,
        "audit_list",
        lambda **_kwargs: [
            {
                "id": 1,
                "request_id": "req-a",
                "execution_id": "exec-a",
                "job_id": None,
                "event_type": "response",
                "level": "info",
                "created_at": "2026-04-15T10:00:01Z",
                "metadata": {"success": True, "document_type": "invoice", "template_used": "invoice"},
            },
            {
                "id": 2,
                "request_id": "req-b",
                "execution_id": "exec-b",
                "job_id": "job-b",
                "event_type": "response",
                "level": "error",
                "created_at": "2026-04-15T10:00:02Z",
                "metadata": {"success": False, "document_type": "policy", "template_used": "policy"},
            },
        ],
    )

    payload = run_async(_audit_api.api_audit_list())

    assert payload["count"] == 2
    assert len(payload["history_summaries"]) == 2
    assert {summary["request_id"] for summary in payload["history_summaries"]} == {"req-a", "req-b"}
