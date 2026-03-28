"""
Tests für T-MKIT-035: SQLite Template Registry + CRUD API.

Alle Tests laufen ohne Docker-Container und ohne echte API-Calls.
DB wird in tempfile.mkdtemp() angelegt und nach jedem Test bereinigt.

Endpoint-Funktionen werden direkt als Coroutinen getestet (kein TestClient nötig).
"""

import json
import sqlite3
import tempfile
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conftest import load_server_module, run_async


# ---------------------------------------------------------------------------
# Server-Modul mit Pass-Through-Dekoratoren (für direkte Endpoint-Tests)
# ---------------------------------------------------------------------------

class _MockHTTPException(Exception):
    """Mock für FastAPI HTTPException, akzeptiert status_code und detail als kwargs."""
    def __init__(self, status_code: int = 500, detail: str = ""):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"{status_code}: {detail}")


def _load_api_server():
    """Lädt server.py mit app.post/get als Pass-Through-Dekoratoren."""
    fastapi_mock = MagicMock()
    app_mock = MagicMock()
    app_mock.post = lambda *a, **kw: (lambda f: f)
    app_mock.get = lambda *a, **kw: (lambda f: f)
    app_mock.put = lambda *a, **kw: (lambda f: f)
    app_mock.delete = lambda *a, **kw: (lambda f: f)
    app_mock.exception_handler = lambda *a, **kw: (lambda f: f)
    fastapi_mock.FastAPI = MagicMock(return_value=app_mock)
    fastapi_mock.HTTPException = _MockHTTPException
    fastapi_mock.Request = MagicMock()

    return load_server_module(
        use_real_pil=False,
        extra_patches={
            "fastapi": fastapi_mock,
            "fastapi.exceptions": MagicMock(),
            "fastapi.responses": MagicMock(),
        },
    )


_server = load_server_module(use_real_pil=False)
_api = _load_api_server()


@pytest.fixture(autouse=True)
def temp_db(tmp_path):
    """Legt eine temporäre DB-Datei für jeden Test an und patcht TEMPLATES_DB_PATH in beiden Modulen."""
    db_path = tmp_path / "test_templates.db"
    with patch.object(_server, "TEMPLATES_DB_PATH", db_path), \
         patch.object(_api, "TEMPLATES_DB_PATH", db_path):
        yield db_path


# ---------------------------------------------------------------------------
# Tests: init_templates_db
# ---------------------------------------------------------------------------


class TestInitTemplatesDb:
    def test_init_creates_db(self, temp_db):
        """DB-Datei wird beim ersten Aufruf erstellt."""
        assert not temp_db.exists()
        _server.init_templates_db()
        assert temp_db.exists()

    def test_init_creates_templates_table(self, temp_db):
        """templates-Tabelle wird korrekt erstellt."""
        _server.init_templates_db()
        conn = sqlite3.connect(str(temp_db))
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='template'")
        result = cursor.fetchone()
        conn.close()
        assert result is not None, "template-Tabelle sollte existieren"

    def test_init_migrates_existing_templates(self, temp_db):
        """invoice, cv, contract werden bei leerem DB migriert."""
        _server.init_templates_db()
        conn = sqlite3.connect(str(temp_db))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT id FROM template").fetchall()
        conn.close()
        ids = {r["id"] for r in rows}
        assert "invoice" in ids
        assert "cv" in ids
        assert "contract" in ids

    def test_init_idempotent(self, temp_db):
        """Doppelter Aufruf von init_templates_db macht nichts kaputt."""
        _server.init_templates_db()
        _server.init_templates_db()  # Zweiter Aufruf
        conn = sqlite3.connect(str(temp_db))
        cursor = conn.execute("SELECT COUNT(*) FROM template")
        count = cursor.fetchone()[0]
        conn.close()
        assert count == 143  # Nur einmal geseeded

    def test_init_migration_schema_valid(self, temp_db):
        """Migrierte Templates haben valide JSON-Schemas."""
        _server.init_templates_db()
        conn = sqlite3.connect(str(temp_db))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT id, schema FROM template").fetchall()
        conn.close()
        for row in rows:
            schema = json.loads(row["schema"])
            assert "type" in schema
            assert "properties" in schema


# ---------------------------------------------------------------------------
# Tests: get_template_by_id
# ---------------------------------------------------------------------------


class TestGetTemplateById:
    def test_get_existing_template(self, temp_db):
        """Bekanntes Template wird korrekt geladen."""
        _server.init_templates_db()
        result = _server.get_template_by_id("invoice")
        assert result is not None
        assert result["id"] == "invoice"
        assert isinstance(result["schema"], dict)
        assert "properties" in result["schema"]

    def test_get_template_not_found(self, temp_db):
        """Unbekanntes Template gibt None zurück."""
        _server.init_templates_db()
        result = _server.get_template_by_id("nonexistent_template_xyz")
        assert result is None

    def test_get_template_disabled(self, temp_db):
        """Disabled Template gibt None zurück."""
        _server.init_templates_db()
        # Template deaktivieren
        conn = sqlite3.connect(str(temp_db))
        conn.execute("UPDATE template SET enabled = 0 WHERE id = 'invoice'")
        conn.commit()
        conn.close()
        result = _server.get_template_by_id("invoice")
        assert result is None

    def test_get_template_schema_deserialized(self, temp_db):
        """Schema-Feld wird als Dict (nicht String) zurückgegeben."""
        _server.init_templates_db()
        result = _server.get_template_by_id("cv")
        assert isinstance(result["schema"], dict), "schema sollte als dict deserialisiert sein"
        assert "properties" in result["schema"]


# ---------------------------------------------------------------------------
# Tests: REST API — POST /v1/templates (create)
# ---------------------------------------------------------------------------


class TestCreateTemplate:
    def test_create_template_success(self, temp_db):
        """api_create_template erstellt ein neues Template."""
        _api.init_templates_db()
        payload = {
            "id": "test_tmpl",
            "schema": {"type": "object", "properties": {"foo": {"type": "string"}}},
            "display_name": "Test Template",
            "category": "test",
        }
        result = run_async(_api.api_create_template(payload))
        assert result["success"] is True
        assert result["id"] == "test_tmpl"

        # Verify it's in the DB
        tmpl = _api.get_template_by_id("test_tmpl")
        assert tmpl is not None
        assert tmpl["schema"]["properties"]["foo"]["type"] == "string"

    def test_create_template_missing_id(self, temp_db):
        """api_create_template ohne id wirft HTTPException mit status_code 400."""
        _api.init_templates_db()
        with pytest.raises(_MockHTTPException) as exc_info:
            run_async(_api.api_create_template({"schema": {"type": "object"}}))
        assert exc_info.value.status_code == 400

    def test_create_template_missing_schema(self, temp_db):
        """api_create_template ohne schema wirft HTTPException mit status_code 400."""
        _api.init_templates_db()
        with pytest.raises(_MockHTTPException) as exc_info:
            run_async(_api.api_create_template({"id": "tmpl_no_schema"}))
        assert exc_info.value.status_code == 400

    def test_create_duplicate_gives_409(self, temp_db):
        """api_create_template mit doppelter ID wirft HTTPException mit status_code 409."""
        _api.init_templates_db()
        payload = {
            "id": "invoice",
            "schema": {"type": "object", "properties": {}},
        }
        with pytest.raises(_MockHTTPException) as exc_info:
            run_async(_api.api_create_template(payload))
        assert exc_info.value.status_code == 409


# ---------------------------------------------------------------------------
# Tests: REST API — PUT /v1/templates/{id} (update)
# ---------------------------------------------------------------------------


class TestUpdateTemplate:
    def test_update_template_partial(self, temp_db):
        """api_update_template aktualisiert Felder des Templates."""
        _api.init_templates_db()
        result = run_async(_api.api_update_template("invoice", {"display_name": "Updated Invoice"}))
        assert result["success"] is True

        # Verify updated in DB
        conn = sqlite3.connect(str(temp_db))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT display_name FROM template WHERE id = 'invoice'").fetchone()
        conn.close()
        assert row["display_name"] == "Updated Invoice"

    def test_update_nonexistent_template_gives_404(self, temp_db):
        """api_update_template für unbekanntes Template wirft HTTPException mit status_code 404."""
        _api.init_templates_db()
        with pytest.raises(_MockHTTPException) as exc_info:
            run_async(_api.api_update_template("nonexistent_xyz", {"display_name": "X"}))
        assert exc_info.value.status_code == 404

    def test_update_schema_field(self, temp_db):
        """api_update_template kann das schema-Feld aktualisieren."""
        _api.init_templates_db()
        new_schema = {"type": "object", "properties": {"new_field": {"type": "string"}}}
        result = run_async(_api.api_update_template("contract", {"schema": new_schema}))
        assert result["success"] is True

        tmpl = _api.get_template_by_id("contract")
        assert "new_field" in tmpl["schema"]["properties"]


# ---------------------------------------------------------------------------
# Tests: REST API — DELETE /v1/templates/{id}
# ---------------------------------------------------------------------------


class TestDeleteTemplate:
    def test_delete_template(self, temp_db):
        """api_delete_template löscht das Template."""
        _api.init_templates_db()
        result = run_async(_api.api_delete_template("cv"))
        assert result["success"] is True

        # Verify gone from DB
        tmpl = _api.get_template_by_id("cv")
        assert tmpl is None

    def test_delete_nonexistent_gives_404(self, temp_db):
        """api_delete_template für unbekanntes Template wirft HTTPException mit status_code 404."""
        _api.init_templates_db()
        with pytest.raises(_MockHTTPException) as exc_info:
            run_async(_api.api_delete_template("nonexistent_xyz"))
        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# Tests: REST API — POST /v1/templates/bulk
# ---------------------------------------------------------------------------


class TestBulkTemplates:
    def test_bulk_upsert_creates_new(self, temp_db):
        """api_bulk_templates erstellt neue Templates."""
        _api.init_templates_db()
        payload = {
            "mode": "upsert",
            "templates": [
                {
                    "id": "bulk_a",
                    "schema": {"type": "object", "properties": {"x": {"type": "string"}}},
                },
                {
                    "id": "bulk_b",
                    "schema": {"type": "object", "properties": {"y": {"type": "number"}}},
                },
            ],
        }
        result = run_async(_api.api_bulk_templates(payload))
        assert result["success"] is True
        assert result["created"] == 2
        assert result["updated"] == 0

    def test_bulk_upsert_updates_existing(self, temp_db):
        """api_bulk_templates aktualisiert bestehende Templates."""
        _api.init_templates_db()
        payload = {
            "mode": "upsert",
            "templates": [
                {
                    "id": "invoice",
                    "schema": {"type": "object", "properties": {"invoice_number": {"type": "string"}}},
                    "display_name": "Updated Invoice",
                },
            ],
        }
        result = run_async(_api.api_bulk_templates(payload))
        assert result["updated"] == 1
        assert result["created"] == 0

    def test_bulk_skips_entries_without_required_fields(self, temp_db):
        """Einträge ohne id oder schema werden übersprungen."""
        _api.init_templates_db()
        payload = {
            "templates": [
                {"id": "missing_schema"},  # kein schema
                {"schema": {"type": "object"}},  # keine id
            ]
        }
        result = run_async(_api.api_bulk_templates(payload))
        assert result["created"] == 0
        assert result["updated"] == 0


# ---------------------------------------------------------------------------
# Tests: REST API — GET /v1/templates/categories
# ---------------------------------------------------------------------------


class TestTemplateCategories:
    def test_categories_returns_seed_categories(self, temp_db):
        """api_template_categories gibt die Seed-Kategorien zurück."""
        _api.init_templates_db()
        result = run_async(_api.api_template_categories())
        assert "categories" in result
        category_names = {c["name"] for c in result["categories"]}
        # 137 Templates in 13 Kategorien (A-M)
        assert len(category_names) >= 10
        assert any("Finanzen" in c for c in category_names)

    def test_categories_count_matches_templates(self, temp_db):
        """Kategorie-Count entspricht Anzahl Templates in der Kategorie."""
        _api.init_templates_db()
        result = run_async(_api.api_template_categories())
        total = sum(c["count"] for c in result["categories"])
        assert total == 143


# ---------------------------------------------------------------------------
# Tests: REST API — GET /v1/templates/search
# ---------------------------------------------------------------------------


class TestSearchTemplates:
    def test_search_finds_by_id(self, temp_db):
        """api_search_templates?q=invoice findet invoice-Template."""
        _api.init_templates_db()
        result = run_async(_api.api_search_templates(q="invoice"))
        assert "templates" in result
        ids = [t["id"] for t in result["templates"]]
        assert "invoice" in ids

    def test_search_empty_query_returns_empty(self, temp_db):
        """api_search_templates ohne q gibt leere Liste zurück."""
        _api.init_templates_db()
        result = run_async(_api.api_search_templates(q=""))
        assert result["templates"] == []

    def test_search_no_match_returns_empty(self, temp_db):
        """Suche ohne Treffer gibt leere Liste zurück."""
        _api.init_templates_db()
        result = run_async(_api.api_search_templates(q="zzz_nothing_matches_this_xyz"))
        assert result["templates"] == []


# ---------------------------------------------------------------------------
# Tests: GET /v1/templates/{template_id}
# ---------------------------------------------------------------------------


class TestGetTemplateEndpoint:
    def test_get_existing_template_endpoint(self, temp_db):
        """api_get_template gibt Template-Details zurück."""
        _api.init_templates_db()
        result = run_async(_api.api_get_template("contract"))
        assert result["id"] == "contract"
        assert "schema" in result

    def test_get_nonexistent_template_gives_404(self, temp_db):
        """api_get_template für unbekanntes Template wirft HTTPException mit status_code 404."""
        _api.init_templates_db()
        with pytest.raises(_MockHTTPException) as exc_info:
            run_async(_api.api_get_template("does_not_exist_xyz"))
        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# Tests: Template in extract-Pipeline
# ---------------------------------------------------------------------------


class TestTemplateUsedInExtract:
    def test_extract_via_db_template(self, temp_db):
        """extract mit Template aus DB funktioniert korrekt."""
        _server.init_templates_db()

        extracted_data = {"invoice_number": "DB-001", "total_amount": 99.0, "vendor": "Test GmbH"}

        def _make_mistral_response(data):
            return {
                "choices": [{"message": {"content": json.dumps(data)}}],
                "usage": {"total_tokens": 10},
            }

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "call_mistral_vision_api",
                          new=AsyncMock(return_value=_make_mistral_response(extracted_data))), \
             patch.object(_server, "convert_with_markitdown",
                          return_value={"success": True, "markdown": "# Invoice\nTest"}), \
             patch.object(_server, "detect_mimetype_from_bytes",
                          return_value="text/plain"):
            result = run_async(_server.convert_auto(
                file_data=b"invoice content",
                filename="invoice.txt",
                source="test",
                source_type="file",
                input_meta={},
                extract_schema=_server.get_template_by_id("invoice")["schema"],
            ))

        assert result.success is True
        assert result.extracted is not None
        assert result.extracted["invoice_number"] == "DB-001"

    def test_get_template_by_id_returns_correct_schema(self, temp_db):
        """get_template_by_id liefert Schema aus DB."""
        _server.init_templates_db()

        # Überschreibe invoice in DB mit anderem Schema
        conn = sqlite3.connect(str(temp_db))
        new_schema = {"type": "object", "properties": {"custom_field": {"type": "string"}}}
        conn.execute("UPDATE template SET schema = ? WHERE id = 'invoice'", (json.dumps(new_schema),))
        conn.commit()
        conn.close()

        result = _server.get_template_by_id("invoice")
        assert result is not None
        assert "custom_field" in result["schema"]["properties"]
