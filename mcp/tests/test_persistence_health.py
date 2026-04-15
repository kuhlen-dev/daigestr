"""
Tests for E8/W8.1/T8.1.2: DB-backed health/readiness semantics.
"""

from unittest.mock import MagicMock, patch

from conftest import load_server_module, run_async


_server = load_server_module(use_real_pil=False)


def _load_api_server():
    """Load server.py with pass-through FastAPI decorators for endpoint testing."""
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


class _Conn:
    def __init__(self, rows=None):
        self._rows = rows or []
        self.status = 1

    def cursor(self):
        return _Cursor(self._rows)

    def rollback(self):
        return None


class _Cursor:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, *_args, **_kwargs):
        return None

    def fetchone(self):
        return {"ok": 1}

    def fetchall(self):
        return self._rows


class TestCheckPersistenceHealth:
    def test_check_persistence_health_ready_when_all_required_tables_present(self):
        rows = [
            {"tablename": "template"},
            {"tablename": "prompt"},
            {"tablename": "scoring_weight"},
            {"tablename": "cache"},
            {"tablename": "job"},
            {"tablename": "execution"},
            {"tablename": "execution_attempt"},
            {"tablename": "execution_subjob"},
            {"tablename": "execution_result"},
            {"tablename": "execution_batch"},
            {"tablename": "execution_batch_item"},
            {"tablename": "execution_queue"},
            {"tablename": "audit_log"},
            {"tablename": "normalized_categories"},
            {"tablename": "normalized_fields"},
            {"tablename": "normalized_values"},
            {"tablename": "normalized_test_fixtures"},
            {"tablename": "extraction_corrections"},
            {"tablename": "debug_snapshot"},
        ]
        conn = _Conn(rows=rows)

        with patch.object(_server, "get_db_connection", return_value=conn), \
             patch("templates_db._return_conn", MagicMock()):
            result = _server.check_persistence_health()

        assert result["ready"] is True
        assert result["connection_ok"] is True
        assert result["missing_tables"] == []

    def test_check_persistence_health_reports_connection_failure(self):
        with patch.object(_server, "get_db_connection", side_effect=RuntimeError("db unavailable")):
            result = _server.check_persistence_health()

        assert result["ready"] is False
        assert result["connection_ok"] is False
        assert "db unavailable" in result["error"]


class TestApiHealthPersistence:
    def test_api_health_reports_ok_when_persistence_ready(self):
        with patch.object(_server_api, "check_persistence_health", return_value={
            "ready": True,
            "database_url_configured": True,
            "connection_ok": True,
            "missing_tables": [],
        }):
            response = run_async(_server_api.api_health())

        assert response.status == "ok"
        assert response.meta["persistence_ready"] is True
        assert response.meta["database_connection_ok"] is True
        assert response.meta["execution_result_retention_days"] == _server_api.EXECUTION_RESULT_RETENTION_DAYS
        assert response.meta["execution_result_artifact_retention_days"] == _server_api.EXECUTION_RESULT_ARTIFACT_RETENTION_DAYS
        assert response.meta["debug_snapshot_retention_days"] == _server_api.DEBUG_SNAPSHOTS_RETENTION_DAYS
        assert response.meta["retention_policy"]["execution_metadata"]["retention_days"] is None
        assert response.meta["retention_policy"]["result_payload"]["retention_days"] == _server_api.EXECUTION_RESULT_RETENTION_DAYS
        assert response.meta["pii_policy"]["storage_mode"] == _server_api.PII_STORAGE_MODE
        assert response.meta["pii_policy"]["debug_snapshots_allow_pii"] == _server_api.DEBUG_SNAPSHOTS_ALLOW_PII
        assert response.meta["operator_policy"]["audit_api_enabled"] == _server_api.AUDIT_API_ENABLED
        assert response.meta["operator_policy"]["debug_snapshot_api_enabled"] == _server_api.DEBUG_SNAPSHOT_API_ENABLED

    def test_api_health_reports_error_when_persistence_not_ready(self):
        with patch.object(_server_api, "check_persistence_health", return_value={
            "ready": False,
            "database_url_configured": True,
            "connection_ok": False,
            "missing_tables": ["template"],
            "error": "db down",
        }):
            response = run_async(_server_api.api_health())

        assert response.status == "error"
        assert response.meta["persistence_ready"] is False
        assert response.meta["missing_tables"] == ["template"]
        assert response.meta["persistence_error"] == "db down"
