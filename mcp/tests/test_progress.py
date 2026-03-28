"""
Tests für T-DAI-023: Async Job API + Webhook.

Testet:
- Job API: create → status queued → update progress → set result → get result → delete
- Webhook: Mock-Server, webhook_url gesetzt → POST wird gemacht
- Job list: mehrere Jobs → alle gelistet

Alle Tests laufen ohne Docker-Container und ohne echte API-Calls.
"""

import asyncio
import json
import sqlite3
import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conftest import load_server_module

# Server laden (für _fire_webhook und andere Funktionen)
_server = load_server_module(use_real_pil=False)

# templates_db direkt (schon in sys.modules nach load_server_module)
import templates_db as _templates_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _create_job_table(db_path: Path) -> None:
    """Erstellt die job-Tabelle direkt in der Test-DB."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS job (
            id TEXT PRIMARY KEY,
            status TEXT DEFAULT 'queued',
            progress_json TEXT,
            result_json TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()


@pytest.fixture
def db_path(tmp_path):
    """Legt eine temporäre SQLite-DB mit job-Tabelle an."""
    p = tmp_path / "test_progress.db"
    _create_job_table(p)
    return p


def run_async(coro):
    """Führt eine Coroutine synchron aus."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Hilfsfunktionen die get_db_connection patchen
# ---------------------------------------------------------------------------

def _make_get_db(db_path: Path):
    """Erstellt eine get_db_connection-Funktion die an db_path geht."""
    def _get_db():
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn
    return _get_db


def _patch_db(db_path):
    """Context manager: patcht get_db_connection im server-Modul (wo _get() danach sucht)."""
    return patch.object(_server, "get_db_connection", _make_get_db(db_path))


def _job_create(db_path, job_id):
    with _patch_db(db_path):
        return _templates_db.job_create(job_id)


def _job_update(db_path, job_id, status, progress_json=None):
    with _patch_db(db_path):
        return _templates_db.job_update(job_id, status, progress_json)


def _job_set_result(db_path, job_id, result_json):
    with _patch_db(db_path):
        return _templates_db.job_set_result(job_id, result_json)


def _job_get(db_path, job_id):
    with _patch_db(db_path):
        return _templates_db.job_get(job_id)


def _job_delete(db_path, job_id):
    with _patch_db(db_path):
        return _templates_db.job_delete(job_id)


def _job_list(db_path):
    with _patch_db(db_path):
        return _templates_db.job_list()


# ---------------------------------------------------------------------------
# Tests: Job DB-Funktionen
# ---------------------------------------------------------------------------

class TestJobDB:
    def test_job_create_returns_queued(self, db_path):
        """job_create gibt job_id und status='queued' zurück."""
        job_id = str(uuid.uuid4())
        result = _job_create(db_path, job_id)
        assert result["job_id"] == job_id
        assert result["status"] == "queued"

    def test_job_create_persisted(self, db_path):
        """Nach job_create ist der Job in der DB vorhanden."""
        job_id = str(uuid.uuid4())
        _job_create(db_path, job_id)
        job = _job_get(db_path, job_id)
        assert job is not None
        assert job["id"] == job_id
        assert job["status"] == "queued"
        assert job["result_json"] is None

    def test_job_update_status(self, db_path):
        """job_update ändert den Status korrekt."""
        job_id = str(uuid.uuid4())
        _job_create(db_path, job_id)
        _job_update(db_path, job_id, "processing")
        job = _job_get(db_path, job_id)
        assert job["status"] == "processing"

    def test_job_update_with_progress(self, db_path):
        """job_update speichert progress_json korrekt."""
        job_id = str(uuid.uuid4())
        _job_create(db_path, job_id)
        progress = json.dumps({"message": "Converting page 1/5", "percent": 20})
        _job_update(db_path, job_id, "processing", progress)
        job = _job_get(db_path, job_id)
        assert job["status"] == "processing"
        assert job["progress_json"] == progress
        parsed = json.loads(job["progress_json"])
        assert parsed["percent"] == 20

    def test_job_set_result(self, db_path):
        """job_set_result setzt result_json und Status auf 'completed'."""
        job_id = str(uuid.uuid4())
        _job_create(db_path, job_id)
        result_json = json.dumps({"success": True, "markdown": "# Hello", "meta": {}})
        _job_set_result(db_path, job_id, result_json)
        job = _job_get(db_path, job_id)
        assert job["status"] == "completed"
        assert job["result_json"] == result_json

    def test_job_get_not_found(self, db_path):
        """job_get gibt None zurück wenn Job nicht existiert."""
        result = _job_get(db_path, "nonexistent-id")
        assert result is None

    def test_job_delete(self, db_path):
        """job_delete entfernt den Job aus der DB."""
        job_id = str(uuid.uuid4())
        _job_create(db_path, job_id)
        deleted = _job_delete(db_path, job_id)
        assert deleted is True
        job = _job_get(db_path, job_id)
        assert job is None

    def test_job_delete_not_found(self, db_path):
        """job_delete gibt False zurück wenn Job nicht existiert."""
        deleted = _job_delete(db_path, "nonexistent-id")
        assert deleted is False

    def test_job_list_multiple(self, db_path):
        """job_list gibt alle Jobs zurück."""
        ids = [str(uuid.uuid4()) for _ in range(3)]
        for job_id in ids:
            _job_create(db_path, job_id)
        jobs = _job_list(db_path)
        assert len(jobs) >= 3
        listed_ids = [j["id"] for j in jobs]
        for job_id in ids:
            assert job_id in listed_ids

    def test_job_list_empty(self, db_path):
        """job_list gibt leere Liste zurück wenn keine Jobs vorhanden."""
        jobs = _job_list(db_path)
        assert isinstance(jobs, list)

    def test_job_full_lifecycle(self, db_path):
        """Vollständiger Lifecycle: create → processing → result → delete."""
        job_id = str(uuid.uuid4())

        # 1. Create
        result = _job_create(db_path, job_id)
        assert result["status"] == "queued"

        # 2. Update progress
        _job_update(db_path, job_id, "processing", json.dumps({"step": "ocr"}))
        job = _job_get(db_path, job_id)
        assert job["status"] == "processing"

        # 3. Set result
        result_json = json.dumps({"success": True, "markdown": "# Doc", "meta": {}})
        _job_set_result(db_path, job_id, result_json)
        job = _job_get(db_path, job_id)
        assert job["status"] == "completed"
        assert job["result_json"] == result_json

        # 4. Delete
        deleted = _job_delete(db_path, job_id)
        assert deleted is True
        assert _job_get(db_path, job_id) is None


# ---------------------------------------------------------------------------
# Tests: Webhook
# ---------------------------------------------------------------------------

class TestWebhook:
    def _make_mock_httpx(self):
        """Erstellt ein httpx-Mock mit AsyncClient."""
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=MagicMock())
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        mock_httpx = MagicMock()
        mock_httpx.AsyncClient = MagicMock(return_value=mock_client)
        return mock_httpx, mock_client

    def test_webhook_fires_on_convert(self, db_path):
        """_fire_webhook sendet POST an webhook_url mit Ergebnis-JSON."""
        from models import ConvertResponse, MetaData
        import api_rest as _api_rest

        response = ConvertResponse(
            success=True,
            markdown="# Test",
            meta=MetaData()
        )

        posted_data = {}

        async def run():
            mock_httpx, mock_client = self._make_mock_httpx()
            # _fire_webhook verwendet _get("httpx", httpx) → patchen auf _server.httpx
            with patch.object(_server, "httpx", mock_httpx):
                with patch.object(_api_rest, "httpx", mock_httpx):
                    await _api_rest._fire_webhook("http://webhook.test/hook", response)
            posted_data["call_args"] = mock_client.post.call_args

        run_async(run())

        assert posted_data["call_args"] is not None
        call_url = posted_data["call_args"][0][0]
        assert call_url == "http://webhook.test/hook"
        call_kwargs = posted_data["call_args"][1]
        assert "json" in call_kwargs
        assert call_kwargs["json"]["success"] is True
        assert call_kwargs["json"]["markdown"] == "# Test"

    def test_webhook_failure_does_not_raise(self, db_path):
        """_fire_webhook loggt Fehler aber wirft keine Exception."""
        from models import ConvertResponse, MetaData
        import api_rest as _api_rest

        response = ConvertResponse(success=False, meta=MetaData())

        async def run():
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=Exception("Connection refused"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)

            mock_httpx = MagicMock()
            mock_httpx.AsyncClient = MagicMock(return_value=mock_client)

            with patch.object(_server, "httpx", mock_httpx):
                with patch.object(_api_rest, "httpx", mock_httpx):
                    await _api_rest._fire_webhook("http://broken.test/hook", response)

        run_async(run())

    def test_webhook_payload_structure(self, db_path):
        """Webhook-Payload hat die korrekten ConvertResponse-Felder."""
        from models import ConvertResponse, MetaData, ErrorDetail
        import api_rest as _api_rest

        response = ConvertResponse(
            success=False,
            error=ErrorDetail(code="CONVERSION_FAILED", message="Test error"),
            meta=MetaData(source="test.pdf", format="pdf")
        )

        captured = {}

        async def run():
            mock_httpx, mock_client = self._make_mock_httpx()

            with patch.object(_server, "httpx", mock_httpx):
                with patch.object(_api_rest, "httpx", mock_httpx):
                    await _api_rest._fire_webhook("http://test.local/wh", response)

            captured["call_args"] = mock_client.post.call_args

        run_async(run())

        assert captured["call_args"] is not None
        payload = captured["call_args"][1]["json"]
        assert payload["success"] is False
        assert payload["error"]["code"] == "CONVERSION_FAILED"
        assert payload["meta"]["source"] == "test.pdf"
