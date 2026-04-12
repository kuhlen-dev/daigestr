"""
Tests für T-MKIT-036: Auto-Extract (classify → DB-Lookup → Extraktion).

Alle Tests laufen ohne Docker-Container und ohne echte API-Calls.
Alle externen Abhängigkeiten werden per unittest.mock gemockt.
"""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conftest import load_server_module, run_async


# Einmal laden; alle Tests in diesem Modul teilen diese Instanz
_server = load_server_module(use_real_pil=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pdf_bytes() -> bytes:
    """Minimal-PDF-Datei als Bytes (für Dateityp-Erkennung)."""
    return b"%PDF-1.4 minimal"


def _mock_template(
    template_id: str = "invoice",
    schema: dict | None = None,
    classify_keywords: str = "rechnung,invoice,betrag",
    priority: int = 10,
    version: int = 1,
) -> dict:
    """Erzeugt ein Mock-Template-Dict (wie aus get_template_by_id)."""
    if schema is None:
        schema = {
            "type": "object",
            "properties": {
                "invoice_number": {"type": "string"},
                "total": {"type": "number"},
            }
        }
    return {
        "id": template_id,
        "schema": schema,
        "classify_keywords": classify_keywords,
        "priority": priority,
        "version": version,
        "enabled": 1,
        "display_name": template_id.title(),
        "category": "finance",
        "description": f"Test template {template_id}",
        "field_descriptions": None,
    }


def _make_classify_result(doc_type: str, confidence: float) -> dict:
    return {
        "document_type": doc_type,
        "document_type_confidence": confidence,
    }


def _make_extraction_success(data: dict | None = None) -> dict:
    return {
        "success": True,
        "extracted": data or {"invoice_number": "INV-001", "total": 100.0},
        "tokens": 50,
    }


def _make_extraction_failure(error: str = "LLM Error") -> dict:
    return {
        "success": False,
        "error": error,
        "extracted": None,
        "tokens": 0,
    }


def _make_convert_result(markdown: str = "# Test Document\nRechnung Nr. INV-001") -> dict:
    return {
        "success": True,
        "markdown": markdown,
    }


def _make_quality_meta() -> dict:
    return {"quality_score": 0.9, "quality_grade": "good"}


# ---------------------------------------------------------------------------
# Tests: find_matching_template
# ---------------------------------------------------------------------------

class TestFindMatchingTemplate:
    """Tests für die find_matching_template() Funktion."""

    @staticmethod
    def _conn_with_rows(rows):
        class _Cursor:
            def __init__(self, data):
                self._data = data

            def execute(self, *_args, **_kwargs):
                return None

            def fetchall(self):
                return self._data

        class _Conn:
            status = 1

            def __init__(self, data):
                self._cursor = _Cursor(data)

            def cursor(self):
                return self._cursor

            def rollback(self):
                return None

        return _Conn(rows)

    def test_exact_match_via_get_template_by_id(self):
        """Schritt 1: Exakter Match über get_template_by_id."""
        tmpl = _mock_template("invoice")
        with patch.object(_server, "get_template_by_id", return_value=tmpl):
            result = _server.find_matching_template("invoice", "Some markdown text")
        assert result is not None
        assert result["id"] == "invoice"

    def test_no_exact_match_falls_through_to_keyword(self):
        """Kein exakter Match → Keyword-Suche wird ausgelöst."""
        tmpl = _mock_template("invoice", classify_keywords="rechnung,invoice")
        conn_mock = self._conn_with_rows([
            {
                "id": "invoice",
                "classify_keywords": "rechnung,invoice",
                "priority": 10,
                "schema": json.dumps(tmpl["schema"]),
                "field_descriptions": None,
                "enabled": 1,
            }
        ])
        with patch.object(_server, "get_template_by_id", return_value=None), \
             patch.object(_server, "get_db_connection", return_value=conn_mock):
            result = _server.find_matching_template("unknown_type", "Rechnung Nr. 1234")
        assert result is not None
        assert result["id"] == "invoice"

    def test_no_template_found(self):
        """Kein Template → None."""
        conn_mock = self._conn_with_rows([])
        with patch.object(_server, "get_template_by_id", return_value=None), \
             patch.object(_server, "get_db_connection", return_value=conn_mock):
            result = _server.find_matching_template("other", "Some random text")
        assert result is None

    def test_keyword_match_returns_best_priority(self):
        """Bei mehreren Keyword-Matches: höhere Priority gewinnt."""
        # Template mit Prio 20 soll gewinnen
        low_prio = {
            "id": "generic",
            "classify_keywords": "dokument",
            "priority": 5,
            "schema": json.dumps({"type": "object", "properties": {}}),
            "field_descriptions": None,
            "enabled": 1,
        }
        high_prio = {
            "id": "invoice",
            "classify_keywords": "rechnung,invoice,dokument",
            "priority": 20,
            "schema": json.dumps({"type": "object", "properties": {"total": {"type": "number"}}}),
            "field_descriptions": None,
            "enabled": 1,
        }
        conn_mock = self._conn_with_rows([low_prio, high_prio])
        with patch.object(_server, "get_template_by_id", return_value=None), \
             patch.object(_server, "get_db_connection", return_value=conn_mock):
            result = _server.find_matching_template("unknown", "Rechnung invoice dokument")
        assert result is not None
        assert result["id"] == "invoice"

    def test_keyword_match_same_priority_more_matches_wins(self):
        """Gleiche Priority → mehr Keyword-Matches gewinnt."""
        few_match = {
            "id": "generic",
            "classify_keywords": "dokument",
            "priority": 10,
            "schema": json.dumps({"type": "object", "properties": {}}),
            "field_descriptions": None,
            "enabled": 1,
        }
        more_match = {
            "id": "invoice",
            "classify_keywords": "rechnung,invoice,betrag",
            "priority": 10,
            "schema": json.dumps({"type": "object", "properties": {"total": {"type": "number"}}}),
            "field_descriptions": None,
            "enabled": 1,
        }
        conn_mock = self._conn_with_rows([few_match, more_match])
        with patch.object(_server, "get_template_by_id", return_value=None), \
             patch.object(_server, "get_db_connection", return_value=conn_mock):
            # Text enthält alle 3 Keywords von "invoice" aber nur 1 von "generic"
            result = _server.find_matching_template("unknown", "Rechnung invoice betrag")
        assert result is not None
        assert result["id"] == "invoice"


# ---------------------------------------------------------------------------
# Tests: convert_auto() with auto_extract=True
# ---------------------------------------------------------------------------

class TestAutoExtractInConvertAuto:
    """Integration-Tests für auto_extract in convert_auto()."""

    def _setup_markitdown_convert(self, markdown: str = "# Invoice\nRechnung Nr. 001\nBetrag: 100 EUR"):
        """Richtet standard markitdown-Konvertierungs-Mock ein."""
        result = {"success": True, "markdown": markdown}
        return result

    def test_auto_extract_finds_template_and_extracts(self):
        """auto_extract=True: Template gefunden → extracted enthält strukturierte Daten."""
        tmpl = _mock_template("invoice")
        extracted_data = {"invoice_number": "INV-001", "total": 100.0}
        markdown = "# Rechnung\nInvoice Nr. INV-001\nBetrag: 100 EUR"

        with patch.object(_server, "CACHE_ENABLED", False), \
             patch.object(_server, "convert_with_markitdown", return_value={"success": True, "markdown": markdown}), \
             patch.object(_server, "is_scanned_pdf", return_value=False), \
             patch.object(_server, "get_file_extension", return_value=".pdf"), \
             patch.object(_server, "detect_mimetype_from_bytes", return_value="application/pdf"), \
             patch.object(_server, "get_mimetype", return_value="application/pdf"), \
             patch.object(_server, "calculate_quality_score", return_value={"quality_score": 0.9, "quality_grade": "good"}), \
             patch.object(_server, "classify_document", new=AsyncMock(return_value=_make_classify_result("invoice", 0.92))), \
             patch.object(_server, "find_matching_template", return_value=tmpl), \
             patch.object(_server, "extract_structured_data", new=AsyncMock(return_value=_make_extraction_success(extracted_data))):

            response = run_async(_server.convert_auto(
                file_data=b"%PDF-fake",
                filename="invoice.pdf",
                source="/data/invoice.pdf",
                source_type="file",
                input_meta={},
                auto_extract=True,
            ))

        assert response.success is True
        assert response.extracted == extracted_data
        assert response.meta.template_used == "invoice"
        assert response.meta.document_type == "invoice"

    def test_auto_extract_no_template_returns_error(self):
        """auto_extract=True ohne Template darf keinen Success-Envelope liefern."""
        markdown = "# Unknown Document\nSome content"

        with patch.object(_server, "CACHE_ENABLED", False), \
             patch.object(_server, "convert_with_markitdown", return_value={"success": True, "markdown": markdown}), \
             patch.object(_server, "is_scanned_pdf", return_value=False), \
             patch.object(_server, "get_file_extension", return_value=".pdf"), \
             patch.object(_server, "detect_mimetype_from_bytes", return_value="application/pdf"), \
             patch.object(_server, "get_mimetype", return_value="application/pdf"), \
             patch.object(_server, "calculate_quality_score", return_value={"quality_score": 0.8, "quality_grade": "good"}), \
             patch.object(_server, "classify_document", new=AsyncMock(return_value=_make_classify_result("report", 0.85))), \
             patch.object(_server, "find_matching_template", return_value=None), \
             patch.object(_server, "extract_structured_data", new=AsyncMock(return_value=_make_extraction_success())):

            response = run_async(_server.convert_auto(
                file_data=b"%PDF-fake",
                filename="report.pdf",
                source="/data/report.pdf",
                source_type="file",
                input_meta={},
                auto_extract=True,
            ))

        assert response.success is False
        assert response.extracted is None
        assert response.meta.template_used is None
        assert response.error is not None
        # Hint über fehlendes Template sollte gesetzt sein
        hints = response.meta.hints or []
        assert any("No template registered" in h for h in hints)

    def test_auto_extract_low_confidence_returns_error(self):
        """Konfidenz unter min_confidence → echter Fehler statt leerem Success-Envelope."""
        markdown = "# Some doc"

        with patch.object(_server, "CACHE_ENABLED", False), \
             patch.object(_server, "convert_with_markitdown", return_value={"success": True, "markdown": markdown}), \
             patch.object(_server, "is_scanned_pdf", return_value=False), \
             patch.object(_server, "get_file_extension", return_value=".pdf"), \
             patch.object(_server, "detect_mimetype_from_bytes", return_value="application/pdf"), \
             patch.object(_server, "get_mimetype", return_value="application/pdf"), \
             patch.object(_server, "calculate_quality_score", return_value={"quality_score": 0.7, "quality_grade": "fair"}), \
             patch.object(_server, "classify_document", new=AsyncMock(return_value=_make_classify_result("invoice", 0.5))), \
             patch.object(_server, "find_matching_template", return_value=_mock_template()) as mock_find, \
             patch.object(_server, "extract_structured_data", new=AsyncMock()) as mock_extract:

            response = run_async(_server.convert_auto(
                file_data=b"%PDF-fake",
                filename="doc.pdf",
                source="/data/doc.pdf",
                source_type="file",
                input_meta={},
                auto_extract=True,
                min_confidence=0.7,  # Konfidenz 0.5 < 0.7 → skip
            ))

        assert response.success is False
        assert response.extracted is None
        assert response.error is not None
        mock_find.assert_not_called()
        mock_extract.assert_not_called()

    def test_auto_extract_meta_fields(self):
        """auto_extract setzt template_used und template_version in meta."""
        tmpl = _mock_template("invoice", version=3)
        markdown = "# Rechnung\nInvoice"

        with patch.object(_server, "convert_with_markitdown", return_value={"success": True, "markdown": markdown}), \
             patch.object(_server, "is_scanned_pdf", return_value=False), \
             patch.object(_server, "get_file_extension", return_value=".pdf"), \
             patch.object(_server, "detect_mimetype_from_bytes", return_value="application/pdf"), \
             patch.object(_server, "get_mimetype", return_value="application/pdf"), \
             patch.object(_server, "calculate_quality_score", return_value={"quality_score": 0.9, "quality_grade": "good"}), \
             patch.object(_server, "classify_document", new=AsyncMock(return_value=_make_classify_result("invoice", 0.95))), \
             patch.object(_server, "find_matching_template", return_value=tmpl), \
             patch.object(_server, "extract_structured_data", new=AsyncMock(return_value=_make_extraction_success())):

            response = run_async(_server.convert_auto(
                file_data=b"%PDF-fake",
                filename="invoice.pdf",
                source="/data/invoice.pdf",
                source_type="file",
                input_meta={},
                auto_extract=True,
            ))

        assert response.meta.template_used == "invoice"
        assert response.meta.template_version == 3
        assert response.meta.auto_extract is True

    def test_explicit_template_materializes_canonical_meta_fields(self):
        """Explizites template setzt template_used und template_version auch ohne auto_extract."""
        explicit_schema = {"type": "object", "properties": {"invoice_number": {"type": "string"}}}
        extracted_data = {"invoice_number": "INV-001"}
        markdown = "# Rechnung\nInvoice"
        tmpl = _mock_template("invoice", version=7)

        with patch.object(_server, "CACHE_ENABLED", False), \
             patch.object(_server, "convert_with_markitdown", return_value={"success": True, "markdown": markdown}), \
             patch.object(_server, "is_scanned_pdf", return_value=False), \
             patch.object(_server, "get_file_extension", return_value=".pdf"), \
             patch.object(_server, "detect_mimetype_from_bytes", return_value="application/pdf"), \
             patch.object(_server, "get_mimetype", return_value="application/pdf"), \
             patch.object(_server, "get_template_by_id", return_value=tmpl), \
             patch.object(_server, "calculate_quality_score", return_value={"quality_score": 0.9, "quality_grade": "good"}), \
             patch.object(_server, "extract_structured_data", new=AsyncMock(return_value=_make_extraction_success(extracted_data))):

            response = run_async(_server.convert_auto(
                file_data=b"%PDF-fake",
                filename="invoice.pdf",
                source="/data/invoice.pdf",
                source_type="file",
                input_meta={},
                extract_schema=explicit_schema,
                template="invoice",
            ))

        assert response.success is True
        assert response.extracted == extracted_data
        assert response.meta.template_used == "invoice"
        assert response.meta.template_version == 7

    def test_auto_extract_default_false(self):
        """auto_extract defaults to False — keine Klassifizierung ausgelöst."""
        markdown = "# Rechnung"

        with patch.object(_server, "convert_with_markitdown", return_value={"success": True, "markdown": markdown}), \
             patch.object(_server, "is_scanned_pdf", return_value=False), \
             patch.object(_server, "get_file_extension", return_value=".pdf"), \
             patch.object(_server, "detect_mimetype_from_bytes", return_value="application/pdf"), \
             patch.object(_server, "get_mimetype", return_value="application/pdf"), \
             patch.object(_server, "calculate_quality_score", return_value={"quality_score": 0.9, "quality_grade": "good"}), \
             patch.object(_server, "classify_document", new=AsyncMock(return_value=_make_classify_result("invoice", 0.9))) as mock_classify, \
             patch.object(_server, "extract_structured_data", new=AsyncMock()) as mock_extract:

            response = run_async(_server.convert_auto(
                file_data=b"%PDF-fake",
                filename="invoice.pdf",
                source="/data/invoice.pdf",
                source_type="file",
                input_meta={},
                # auto_extract=False is default
            ))

        assert response.success is True
        assert response.extracted is None
        mock_classify.assert_not_called()
        mock_extract.assert_not_called()

    def test_auto_extract_explicit_template_wins(self):
        """Wenn extract_schema gesetzt, wird auto_extract ignoriert (Rückwärtskompatibilität)."""
        explicit_schema = {"type": "object", "properties": {"custom_field": {"type": "string"}}}
        extracted_data = {"custom_field": "value123"}
        markdown = "# Document"

        with patch.object(_server, "convert_with_markitdown", return_value={"success": True, "markdown": markdown}), \
             patch.object(_server, "is_scanned_pdf", return_value=False), \
             patch.object(_server, "get_file_extension", return_value=".pdf"), \
             patch.object(_server, "detect_mimetype_from_bytes", return_value="application/pdf"), \
             patch.object(_server, "get_mimetype", return_value="application/pdf"), \
             patch.object(_server, "calculate_quality_score", return_value={"quality_score": 0.9, "quality_grade": "good"}), \
             patch.object(_server, "classify_document", new=AsyncMock(return_value=_make_classify_result("other", 0.3))), \
             patch.object(_server, "find_matching_template", return_value=None) as mock_find, \
             patch.object(_server, "extract_structured_data", new=AsyncMock(return_value=_make_extraction_success(extracted_data))):

            response = run_async(_server.convert_auto(
                file_data=b"%PDF-fake",
                filename="doc.pdf",
                source="/data/doc.pdf",
                source_type="file",
                input_meta={},
                extract_schema=explicit_schema,
                auto_extract=True,  # wird ignoriert wenn extract_schema gesetzt
            ))

        assert response.success is True
        assert response.extracted == extracted_data
        # find_matching_template wird NICHT aufgerufen (extract_schema hat Vorrang)
        mock_find.assert_not_called()

    def test_auto_extract_hint_when_classify_but_no_auto_extract(self):
        """Wenn classify=true aber kein auto_extract → Hint über auto_extract."""
        markdown = "# Rechnung"

        with patch.object(_server, "convert_with_markitdown", return_value={"success": True, "markdown": markdown}), \
             patch.object(_server, "is_scanned_pdf", return_value=False), \
             patch.object(_server, "get_file_extension", return_value=".pdf"), \
             patch.object(_server, "detect_mimetype_from_bytes", return_value="application/pdf"), \
             patch.object(_server, "get_mimetype", return_value="application/pdf"), \
             patch.object(_server, "calculate_quality_score", return_value={"quality_score": 0.9, "quality_grade": "good"}), \
             patch.object(_server, "classify_document", new=AsyncMock(return_value=_make_classify_result("invoice", 0.9))):

            response = run_async(_server.convert_auto(
                file_data=b"%PDF-fake",
                filename="invoice.pdf",
                source="/data/invoice.pdf",
                source_type="file",
                input_meta={},
                classify=True,
                # auto_extract=False (default)
            ))

        assert response.success is True
        hints = response.meta.hints or []
        assert any("auto_extract=true" in h for h in hints)

    def test_auto_extract_extraction_failure_returns_error_and_preserves_template(self):
        """Wenn extract_structured_data fehlschlägt, bleibt das Template materialisiert und der Response wird fehlerhaft."""
        tmpl = _mock_template("invoice")
        markdown = "# Rechnung\nInvoice"

        with patch.object(_server, "convert_with_markitdown", return_value={"success": True, "markdown": markdown}), \
             patch.object(_server, "is_scanned_pdf", return_value=False), \
             patch.object(_server, "get_file_extension", return_value=".pdf"), \
             patch.object(_server, "detect_mimetype_from_bytes", return_value="application/pdf"), \
             patch.object(_server, "get_mimetype", return_value="application/pdf"), \
             patch.object(_server, "calculate_quality_score", return_value={"quality_score": 0.9, "quality_grade": "good"}), \
             patch.object(_server, "classify_document", new=AsyncMock(return_value=_make_classify_result("invoice", 0.95))), \
             patch.object(_server, "find_matching_template", return_value=tmpl), \
             patch.object(_server, "extract_structured_data", new=AsyncMock(return_value=_make_extraction_failure("API timeout"))):

            response = run_async(_server.convert_auto(
                file_data=b"%PDF-fake",
                filename="invoice.pdf",
                source="/data/invoice.pdf",
                source_type="file",
                input_meta={},
                auto_extract=True,
            ))

        assert response.success is False
        assert response.extracted is None
        assert response.error is not None
        assert response.meta.template_used == "invoice"
        assert response.meta.template_version == 1

    def test_auto_extract_classify_already_done(self):
        """Wenn classify=True UND auto_extract=True → classify nur einmal aufgerufen."""
        tmpl = _mock_template("invoice")
        markdown = "# Rechnung"

        classify_mock = AsyncMock(return_value=_make_classify_result("invoice", 0.9))

        with patch.object(_server, "convert_with_markitdown", return_value={"success": True, "markdown": markdown}), \
             patch.object(_server, "is_scanned_pdf", return_value=False), \
             patch.object(_server, "get_file_extension", return_value=".pdf"), \
             patch.object(_server, "detect_mimetype_from_bytes", return_value="application/pdf"), \
             patch.object(_server, "get_mimetype", return_value="application/pdf"), \
             patch.object(_server, "calculate_quality_score", return_value={"quality_score": 0.9, "quality_grade": "good"}), \
             patch.object(_server, "classify_document", new=classify_mock), \
             patch.object(_server, "find_matching_template", return_value=tmpl), \
             patch.object(_server, "extract_structured_data", new=AsyncMock(return_value=_make_extraction_success())):

            response = run_async(_server.convert_auto(
                file_data=b"%PDF-fake",
                filename="invoice.pdf",
                source="/data/invoice.pdf",
                source_type="file",
                input_meta={},
                classify=True,
                auto_extract=True,
            ))

        # classify_document darf nur einmal aufgerufen werden (aus classify=True Pfad)
        # _apply_auto_extract nutzt das Ergebnis vom classify=True Aufruf
        assert classify_mock.call_count == 1
        assert response.extracted is not None


# ---------------------------------------------------------------------------
# Tests: api_extract() with auto_extract
# ---------------------------------------------------------------------------

class TestApiExtractAutoExtract:
    """Tests für api_extract() mit auto_extract=True."""

    def test_api_extract_auto_extract_no_schema_allowed(self):
        """api_extract mit auto_extract=True braucht kein extract_schema."""
        # Dieser Test prüft nur das Model — keine HTTP-Anfrage nötig
        req = _server.ExtractRequest(
            path="/data/invoice.pdf",
            auto_extract=True,
        )
        assert req.auto_extract is True
        assert req.extract_schema is None

    def test_extract_request_default_auto_extract_false(self):
        """ExtractRequest.auto_extract default ist False."""
        req = _server.ExtractRequest(path="/data/doc.pdf")
        assert req.auto_extract is False

    def test_extract_request_min_confidence_default(self):
        """ExtractRequest.min_confidence default ist 0.7."""
        req = _server.ExtractRequest(path="/data/doc.pdf")
        assert req.min_confidence == 0.7

    def test_convert_request_auto_extract_fields(self):
        """ConvertRequest hat auto_extract und min_confidence Felder."""
        req = _server.ConvertRequest(path="/data/doc.pdf")
        assert hasattr(req, "auto_extract")
        assert hasattr(req, "min_confidence")
        assert req.auto_extract is False
        assert req.min_confidence == 0.7


# ---------------------------------------------------------------------------
# Tests: models.py MetaData fields
# ---------------------------------------------------------------------------

class TestMetaDataFields:
    """Tests für neue MetaData Felder."""

    def test_metadata_has_template_used(self):
        """MetaData hat template_used Feld."""
        meta = _server.MetaData()
        assert hasattr(meta, "template_used")
        assert meta.template_used is None

    def test_metadata_has_template_version(self):
        """MetaData hat template_version Feld."""
        meta = _server.MetaData()
        assert hasattr(meta, "template_version")
        assert meta.template_version is None

    def test_metadata_template_fields_serializable(self):
        """template_used und template_version werden korrekt serialisiert."""
        meta = _server.MetaData(template_used="invoice", template_version=2)
        data = json.loads(meta.model_dump_json())
        assert data["template_used"] == "invoice"
        assert data["template_version"] == 2


def test_auto_extract_failed_logs_request_context():
    meta = {
        "document_type": "invoice",
        "document_type_confidence": 0.95,
    }
    response = _server.create_success_response("ok", meta=meta)
    template_row = {
        "id": "invoice",
        "version": 3,
        "schema": {"type": "object"},
    }

    with patch.object(_server, "find_matching_template", return_value=template_row), \
         patch.object(_server, "extract_structured_data", new=AsyncMock(return_value={"success": False, "error": "boom"})), \
         patch.object(_server.log, "warning") as log_warning:
        run_async(
            _server._apply_auto_extract(
                response,
                meta,
                "Invoice markdown",
                "de",
                0.7,
                [],
                request_id="req-123",
                attempt_number=2,
            )
        )

    log_warning.assert_called_once_with(
        "auto_extract_failed",
        template="invoice",
        doc_type="invoice",
        error="boom",
        request_id="req-123",
        attempt_number=2,
    )
