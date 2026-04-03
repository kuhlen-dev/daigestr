"""
Tests für T-MKIT-035: PostgreSQL Template Registry + CRUD API.

Alle Tests laufen ohne Docker-Container und ohne echte API-Calls.
DB ist die echte PostgreSQL-DB (daigestr auf localhost:15432).
Vor jedem Test werden Tabellen geTRUNCATEd und neu geseeded.
"""

import json
import os
import psycopg2
import psycopg2.extras
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conftest import load_server_module, run_async


_DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://daigestr:daigestr@localhost:15432/daigestr",
)


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


def _get_pg_conn():
    conn = psycopg2.connect(_DB_URL)
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    return conn


# ---------------------------------------------------------------------------
# Fixture: DB truncaten + re-seeden vor jedem Test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_db():
    """Truncate + re-init PostgreSQL DB vor jedem Test."""
    import templates_db as _tdb
    _tdb.pool_reset()
    conn = psycopg2.connect(_DB_URL)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("TRUNCATE TABLE template, prompt, scoring_weight, cache RESTART IDENTITY CASCADE")
    conn.close()
    _tdb.pool_reset()
    _server.init_templates_db()
    yield
    _tdb.pool_reset()


# ---------------------------------------------------------------------------
# Tests: init_templates_db
# ---------------------------------------------------------------------------

class TestInitTemplatesDb:
    def test_init_creates_templates_table(self):
        """templates-Tabelle wird korrekt erstellt."""
        conn = _get_pg_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name='template'"
        )
        result = cur.fetchone()
        conn.close()
        assert result is not None, "template-Tabelle sollte existieren"

    def test_init_migrates_existing_templates(self):
        """invoice, cv, contract werden bei leerem DB migriert."""
        conn = _get_pg_conn()
        cur = conn.cursor()
        cur.execute("SELECT id FROM template")
        ids = {r["id"] for r in cur.fetchall()}
        conn.close()
        assert "invoice" in ids
        assert "cv" in ids
        assert "contract" in ids

    def test_init_idempotent(self):
        """Doppelter Aufruf von init_templates_db macht nichts kaputt."""
        _server.init_templates_db()  # Zweiter Aufruf
        conn = _get_pg_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS cnt FROM template")
        count = cur.fetchone()["cnt"]
        conn.close()
        assert count == 143  # Nur einmal geseeded

    def test_init_migration_schema_valid(self):
        """Migrierte Templates haben valide JSON-Schemas."""
        conn = _get_pg_conn()
        cur = conn.cursor()
        cur.execute("SELECT id, schema FROM template")
        rows = cur.fetchall()
        conn.close()
        for row in rows:
            schema = json.loads(row["schema"])
            assert "type" in schema
            assert "properties" in schema


# ---------------------------------------------------------------------------
# Tests: get_template_by_id
# ---------------------------------------------------------------------------

class TestGetTemplateById:
    def test_get_existing_template(self):
        """Bekanntes Template wird korrekt geladen."""
        result = _server.get_template_by_id("invoice")
        assert result is not None
        assert result["id"] == "invoice"
        assert isinstance(result["schema"], dict)
        assert "properties" in result["schema"]

    def test_get_template_not_found(self):
        """Unbekanntes Template gibt None zurück."""
        result = _server.get_template_by_id("nonexistent_template_xyz")
        assert result is None

    def test_get_template_disabled(self):
        """Disabled Template gibt None zurück."""
        conn = _get_pg_conn()
        cur = conn.cursor()
        cur.execute("UPDATE template SET enabled = 0 WHERE id = 'invoice'")
        conn.commit()
        conn.close()
        result = _server.get_template_by_id("invoice")
        assert result is None

    def test_get_template_schema_deserialized(self):
        """Schema-Feld wird als Dict (nicht String) zurückgegeben."""
        result = _server.get_template_by_id("cv")
        assert isinstance(result["schema"], dict), "schema sollte als dict deserialisiert sein"
        assert "properties" in result["schema"]


# ---------------------------------------------------------------------------
# Tests: REST API — POST /v1/templates (create)
# ---------------------------------------------------------------------------

class TestCreateTemplate:
    def test_create_template_success(self):
        """api_create_template erstellt ein neues Template."""
        payload = {
            "id": "test_tmpl",
            "schema": {"type": "object", "properties": {"foo": {"type": "string"}}},
            "display_name": "Test Template",
            "category": "test",
        }
        result = run_async(_api.api_create_template(payload))
        assert result["success"] is True
        assert result["id"] == "test_tmpl"

        tmpl = _api.get_template_by_id("test_tmpl")
        assert tmpl is not None
        assert tmpl["schema"]["properties"]["foo"]["type"] == "string"

    def test_create_template_missing_id(self):
        """api_create_template ohne id wirft HTTPException mit status_code 400."""
        with pytest.raises(_MockHTTPException) as exc_info:
            run_async(_api.api_create_template({"schema": {"type": "object"}}))
        assert exc_info.value.status_code == 400

    def test_create_template_missing_schema(self):
        """api_create_template ohne schema wirft HTTPException mit status_code 400."""
        with pytest.raises(_MockHTTPException) as exc_info:
            run_async(_api.api_create_template({"id": "tmpl_no_schema"}))
        assert exc_info.value.status_code == 400

    def test_create_duplicate_gives_409(self):
        """api_create_template mit doppelter ID wirft HTTPException mit status_code 409."""
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
    def test_update_template_partial(self):
        """api_update_template aktualisiert Felder des Templates."""
        result = run_async(_api.api_update_template("invoice", {"display_name": "Updated Invoice"}))
        assert result["success"] is True

        conn = _get_pg_conn()
        cur = conn.cursor()
        cur.execute("SELECT display_name FROM template WHERE id = 'invoice'")
        row = cur.fetchone()
        conn.close()
        assert row["display_name"] == "Updated Invoice"

    def test_update_nonexistent_template_gives_404(self):
        """api_update_template für unbekanntes Template wirft HTTPException mit status_code 404."""
        with pytest.raises(_MockHTTPException) as exc_info:
            run_async(_api.api_update_template("nonexistent_xyz", {"display_name": "X"}))
        assert exc_info.value.status_code == 404

    def test_update_schema_field(self):
        """api_update_template kann das schema-Feld aktualisieren."""
        new_schema = {"type": "object", "properties": {"new_field": {"type": "string"}}}
        result = run_async(_api.api_update_template("contract", {"schema": new_schema}))
        assert result["success"] is True

        tmpl = _api.get_template_by_id("contract")
        assert "new_field" in tmpl["schema"]["properties"]


# ---------------------------------------------------------------------------
# Tests: REST API — DELETE /v1/templates/{id}
# ---------------------------------------------------------------------------

class TestDeleteTemplate:
    def test_delete_template(self):
        """api_delete_template löscht das Template."""
        result = run_async(_api.api_delete_template("cv"))
        assert result["success"] is True

        tmpl = _api.get_template_by_id("cv")
        assert tmpl is None

    def test_delete_nonexistent_gives_404(self):
        """api_delete_template für unbekanntes Template wirft HTTPException mit status_code 404."""
        with pytest.raises(_MockHTTPException) as exc_info:
            run_async(_api.api_delete_template("nonexistent_xyz"))
        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# Tests: REST API — POST /v1/templates/bulk
# ---------------------------------------------------------------------------

class TestBulkTemplates:
    def test_bulk_upsert_creates_new(self):
        """api_bulk_templates erstellt neue Templates."""
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

    def test_bulk_upsert_updates_existing(self):
        """api_bulk_templates aktualisiert bestehende Templates."""
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

    def test_bulk_skips_entries_without_required_fields(self):
        """Einträge ohne id oder schema werden übersprungen."""
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
    def test_categories_returns_seed_categories(self):
        """api_template_categories gibt die Seed-Kategorien zurück."""
        result = run_async(_api.api_template_categories())
        assert "categories" in result
        category_names = {c["name"] for c in result["categories"]}
        assert len(category_names) >= 10
        assert any("Finanzen" in c for c in category_names)

    def test_categories_count_matches_templates(self):
        """Kategorie-Count entspricht Anzahl Templates in der Kategorie."""
        result = run_async(_api.api_template_categories())
        total = sum(c["count"] for c in result["categories"])
        assert total == 143


# ---------------------------------------------------------------------------
# Tests: REST API — GET /v1/templates/search
# ---------------------------------------------------------------------------

class TestSearchTemplates:
    def test_search_finds_by_id(self):
        """api_search_templates?q=invoice findet invoice-Template."""
        result = run_async(_api.api_search_templates(q="invoice"))
        assert "templates" in result
        ids = [t["id"] for t in result["templates"]]
        assert "invoice" in ids

    def test_search_empty_query_returns_empty(self):
        """api_search_templates ohne q gibt leere Liste zurück."""
        result = run_async(_api.api_search_templates(q=""))
        assert result["templates"] == []

    def test_search_no_match_returns_empty(self):
        """Suche ohne Treffer gibt leere Liste zurück."""
        result = run_async(_api.api_search_templates(q="zzz_nothing_matches_this_xyz"))
        assert result["templates"] == []


# ---------------------------------------------------------------------------
# Tests: GET /v1/templates/{template_id}
# ---------------------------------------------------------------------------

class TestGetTemplateEndpoint:
    def test_get_existing_template_endpoint(self):
        """api_get_template gibt Template-Details zurück."""
        result = run_async(_api.api_get_template("contract"))
        assert result["id"] == "contract"
        assert "schema" in result

    def test_get_nonexistent_template_gives_404(self):
        """api_get_template für unbekanntes Template wirft HTTPException mit status_code 404."""
        with pytest.raises(_MockHTTPException) as exc_info:
            run_async(_api.api_get_template("does_not_exist_xyz"))
        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# Tests: Template in extract-Pipeline
# ---------------------------------------------------------------------------

class TestTemplateUsedInExtract:
    def test_extract_via_db_template(self):
        """extract mit Template aus DB funktioniert korrekt."""
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

    def test_get_template_by_id_returns_correct_schema(self):
        """get_template_by_id liefert Schema aus DB."""
        conn = _get_pg_conn()
        cur = conn.cursor()
        new_schema = {"type": "object", "properties": {"custom_field": {"type": "string"}}}
        cur.execute("UPDATE template SET schema = %s WHERE id = 'invoice'", (json.dumps(new_schema),))
        conn.commit()
        conn.close()

        result = _server.get_template_by_id("invoice")
        assert result is not None
        assert "custom_field" in result["schema"]["properties"]
