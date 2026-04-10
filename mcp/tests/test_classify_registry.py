"""
Tests für T-MKIT-038: classify_document() nutzt Template-Registry IDs statt hardcodierter Kategorien.

Alle Tests laufen ohne Docker-Container und ohne echte API-Calls.
Alle externen Abhängigkeiten werden per unittest.mock gemockt.
"""

import json
import sqlite3
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conftest import load_server_module, run_async


# Einmal laden; alle Tests in diesem Modul teilen diese Instanz
_server = load_server_module(use_real_pil=False)
classify_document = _server.classify_document
get_classify_categories_from_db = _server.get_classify_categories_from_db


def _make_mistral_response(doc_type: str, confidence: float) -> dict:
    """Erzeugt eine minimale Mistral-API-Antwort mit JSON im Content."""
    return {
        "choices": [
            {
                "message": {
                    "content": json.dumps({"type": doc_type, "confidence": confidence})
                }
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def _mock_prompt(category: str, name: str, language: str = "de") -> str:
    """Minimale DB-Prompts für classify_document-Tests ohne echte Prompt-Tabelle."""
    prompts = {
        ("classify", "system_de"): "Du bist ein Experte für Dokumentenklassifizierung. Antworte ausschließlich mit validem JSON.",
        ("classify", "user_de"): (
            "Klassifiziere dieses Dokument.\n"
            "Verfügbare Typen (ID: Beschreibung):\n{categories_lines}\n\n"
            "Dokument:\n{truncated_markdown}"
        ),
        ("classify", "system_en"): "You are an expert document classifier. Respond exclusively with valid JSON.",
        ("classify", "user_en"): (
            "Classify this document.\n"
            "Available types (ID: description):\n{categories_lines}\n\n"
            "Document:\n{truncated_markdown}"
        ),
    }
    return prompts[(category, name)]


def _make_db_with_templates(templates: list[dict]) -> sqlite3.Connection:
    """Erstellt eine In-Memory SQLite-DB mit Templates."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE template (
            id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            category TEXT,
            description TEXT,
            schema TEXT,
            enabled INTEGER DEFAULT 1,
            priority INTEGER DEFAULT 0
        )
    """)
    for t in templates:
        conn.execute(
            "INSERT INTO template (id, display_name, category, enabled, priority) VALUES (?, ?, ?, ?, ?)",
            (t["id"], t["display_name"], t.get("category", ""), t.get("enabled", 1), t.get("priority", 0))
        )
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGetClassifyCategoriesFromDb:
    """Tests für die neue get_classify_categories_from_db() Funktion."""

    def setup_method(self):
        """Cache vor jedem Test leeren."""
        _server._classify_categories_cache["categories"] = None
        _server._classify_categories_cache["timestamp"] = 0

    def test_classify_uses_db_categories(self):
        """DB-Kategorien erscheinen im Prompt statt hardcodierter DEFAULT_CLASSIFY_CATEGORIES."""
        db_templates = [
            {"id": "reimbursement_notice", "display_name": "Beihilfebescheid", "priority": 10},
            {"id": "supplier_invoice", "display_name": "Lieferantenrechnung", "priority": 5},
        ]
        mock_conn = _make_db_with_templates(db_templates)

        with patch.object(_server, "get_db_connection", return_value=mock_conn):
            categories = get_classify_categories_from_db()

        assert any("reimbursement_notice" in c for c in categories), f"Expected reimbursement_notice in {categories}"
        assert any("Beihilfebescheid" in c for c in categories), f"Expected Beihilfebescheid in {categories}"
        assert any("supplier_invoice" in c for c in categories), f"Expected supplier_invoice in {categories}"
        assert any("Lieferantenrechnung" in c for c in categories), f"Expected Lieferantenrechnung in {categories}"

    def test_classify_raises_when_no_templates_enabled(self):
        """Leere Registry ist ein Konfigurationsfehler, kein stiller Fallback."""
        mock_conn = _make_db_with_templates([])

        with patch.object(_server, "get_db_connection", return_value=mock_conn):
            with pytest.raises(RuntimeError, match="Keine aktivierten Templates"):
                get_classify_categories_from_db()

    def test_classify_propagates_db_error(self):
        """DB-Fehler bleiben sichtbar statt auf Default-Kategorien zurückzufallen."""
        with patch.object(_server, "get_db_connection", side_effect=Exception("DB not available")):
            with pytest.raises(Exception, match="DB not available"):
                get_classify_categories_from_db()

    def test_classify_cache(self):
        """Zweiter Aufruf nutzt Cache, nicht die DB."""
        db_templates = [
            {"id": "invoice", "display_name": "Rechnung"},
        ]
        mock_conn = _make_db_with_templates(db_templates)

        with patch.object(_server, "get_db_connection", return_value=mock_conn) as mock_get_conn:
            # Erster Aufruf: DB wird abgefragt
            categories1 = get_classify_categories_from_db()
            # Zweiter Aufruf: Cache wird genutzt
            categories2 = get_classify_categories_from_db()

        # get_db_connection sollte nur EINMAL aufgerufen worden sein
        assert mock_get_conn.call_count == 1, f"Expected 1 DB call, got {mock_get_conn.call_count}"
        assert categories1 == categories2

    def test_classify_cache_expires(self):
        """Cache wird nach TTL-Ablauf erneuert."""
        db_templates = [{"id": "invoice", "display_name": "Rechnung"}]
        mock_conn1 = _make_db_with_templates(db_templates)
        mock_conn2 = _make_db_with_templates(db_templates)

        with patch.object(_server, "get_db_connection", side_effect=[mock_conn1, mock_conn2]) as mock_get_conn:
            # Erster Aufruf
            get_classify_categories_from_db()
            # Cache-Timestamp auf abgelaufen setzen
            _server._classify_categories_cache["timestamp"] = time.time() - _server._CLASSIFY_CACHE_TTL - 1
            # Zweiter Aufruf: Cache abgelaufen, DB erneut abfragen
            get_classify_categories_from_db()

        assert mock_get_conn.call_count == 2, f"Expected 2 DB calls after cache expiry, got {mock_get_conn.call_count}"

    def test_categories_format_id_colon_display_name(self):
        """Kategorien haben das Format 'id: display_name'."""
        db_templates = [
            {"id": "reimbursement_notice", "display_name": "Beihilfebescheid"},
        ]
        mock_conn = _make_db_with_templates(db_templates)

        with patch.object(_server, "get_db_connection", return_value=mock_conn):
            categories = get_classify_categories_from_db()

        assert "reimbursement_notice: Beihilfebescheid" in categories


class TestClassifyDocumentWithRegistry:
    """Tests für classify_document() mit Template-Registry Integration."""

    def setup_method(self):
        """Cache vor jedem Test leeren."""
        _server._classify_categories_cache["categories"] = None
        _server._classify_categories_cache["timestamp"] = 0

    def test_classify_returns_registry_type(self):
        """LLM gibt Registry-Typ zurück (z.B. 'reimbursement_notice')."""
        db_templates = [
            {"id": "reimbursement_notice", "display_name": "Beihilfebescheid"},
            {"id": "invoice", "display_name": "Rechnung"},
            {"id": "other", "display_name": "Sonstige"},
        ]
        mock_conn = _make_db_with_templates(db_templates)
        api_response = _make_mistral_response("reimbursement_notice", 0.93)

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "get_prompt", side_effect=_mock_prompt), \
             patch.object(_server, "get_db_connection", return_value=mock_conn), \
             patch.object(_server, "call_mistral_vision_api", new=AsyncMock(return_value=api_response)):
            result = run_async(classify_document(
                "Beihilfebescheid der Beihilfestelle. Aktenzeichen: 12345"
            ))

        assert result["document_type"] == "reimbursement_notice"
        assert abs(result["document_type_confidence"] - 0.93) < 1e-9

    def test_classify_unknown_type_fallback(self):
        """Unbekannter Typ vom LLM → Fallback zu 'other'."""
        db_templates = [
            {"id": "invoice", "display_name": "Rechnung"},
            {"id": "other", "display_name": "Sonstige"},
        ]
        mock_conn = _make_db_with_templates(db_templates)
        # LLM gibt Typ zurück der nicht in der Registry ist
        api_response = _make_mistral_response("completely_unknown_type", 0.8)

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "get_prompt", side_effect=_mock_prompt), \
             patch.object(_server, "get_db_connection", return_value=mock_conn), \
             patch.object(_server, "call_mistral_vision_api", new=AsyncMock(return_value=api_response)):
            result = run_async(classify_document("Beliebiges Dokument"))

        assert result["document_type"] == "other"

    def test_classify_explicit_categories_override(self):
        """Explizite Kategorien vom Aufrufer überschreiben DB-Kategorien."""
        db_templates = [
            {"id": "invoice", "display_name": "Rechnung"},
            {"id": "reimbursement_notice", "display_name": "Beihilfebescheid"},
        ]
        mock_conn = _make_db_with_templates(db_templates)
        # Explizite Kategorien die NICHT in der DB sind
        explicit_categories = ["receipt", "memo", "report"]
        api_response = _make_mistral_response("memo", 0.88)

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "get_prompt", side_effect=_mock_prompt), \
             patch.object(_server, "get_db_connection", return_value=mock_conn), \
             patch.object(_server, "get_classify_categories_from_db", return_value=[]) as mock_get_cats, \
             patch.object(_server, "call_mistral_vision_api", new=AsyncMock(return_value=api_response)) as mock_api:
            result = run_async(classify_document(
                "Kurze Mitteilung an alle ...",
                categories=explicit_categories,
            ))

        # Explizite Kategorien wurden verwendet → LLM-Typ ist in explicit_categories → nicht zu "other" normalisiert
        assert result["document_type"] == "memo"
        # get_classify_categories_from_db soll NICHT aufgerufen werden wenn explizite Kategorien übergeben wurden
        mock_get_cats.assert_not_called()

        # Prompt enthält explizite Kategorien
        call_payload = mock_api.call_args[0][0]
        user_content = call_payload["messages"][1]["content"]
        for cat in explicit_categories:
            assert cat in user_content

    def test_classify_db_categories_in_prompt(self):
        """DB-Kategorien (ID + display_name) erscheinen im Prompt."""
        db_templates = [
            {"id": "reimbursement_notice", "display_name": "Beihilfebescheid"},
            {"id": "supplier_invoice", "display_name": "Lieferantenrechnung"},
            {"id": "other", "display_name": "Sonstige"},
        ]
        mock_conn = _make_db_with_templates(db_templates)
        api_response = _make_mistral_response("other", 0.5)

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "get_prompt", side_effect=_mock_prompt), \
             patch.object(_server, "get_db_connection", return_value=mock_conn), \
             patch.object(_server, "call_mistral_vision_api", new=AsyncMock(return_value=api_response)) as mock_api:
            run_async(classify_document("Testdokument"))

        call_payload = mock_api.call_args[0][0]
        user_content = call_payload["messages"][1]["content"]

        # IDs und display_names müssen im Prompt erscheinen
        assert "reimbursement_notice" in user_content
        assert "Beihilfebescheid" in user_content
        assert "supplier_invoice" in user_content
        assert "Lieferantenrechnung" in user_content

    def test_classify_uses_db_not_defaults_when_db_has_data(self):
        """Wenn DB Templates hat → DEFAULT_CLASSIFY_CATEGORIES werden NICHT verwendet (nur DB)."""
        db_templates = [
            {"id": "reimbursement_notice", "display_name": "Beihilfebescheid"},
            {"id": "other", "display_name": "Sonstige"},
        ]
        mock_conn = _make_db_with_templates(db_templates)
        # LLM gibt einen hardcodierten Default-Typ zurück der NICHT in der DB ist
        api_response = _make_mistral_response("cv", 0.9)  # "cv" ist in DEFAULT aber nicht in DB

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "get_prompt", side_effect=_mock_prompt), \
             patch.object(_server, "get_db_connection", return_value=mock_conn), \
             patch.object(_server, "call_mistral_vision_api", new=AsyncMock(return_value=api_response)):
            result = run_async(classify_document("Lebenslauf von Max Mustermann"))

        # "cv" ist nicht in der DB → muss zu "other" normalisiert werden
        assert result["document_type"] == "other"
