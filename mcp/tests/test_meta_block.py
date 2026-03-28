"""
Tests für T-MKIT-037: Globaler _meta Block (Steuerrelevanz) + Datentyp-Konventionen.

Alle Tests laufen ohne Docker-Container und ohne echte API-Calls.
Alle externen Abhängigkeiten werden per unittest.mock gemockt.
"""

import json
import sys
from unittest.mock import AsyncMock, patch

import pytest

from conftest import load_server_module, run_async


# Einmal laden; alle Tests in diesem Modul teilen diese Instanz
_server = load_server_module(use_real_pil=False)
extract_structured_data = _server.extract_structured_data


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def _make_mistral_response(data: dict, tokens: int = 20) -> dict:
    """Erzeugt eine minimale Mistral-API-Antwort mit JSON im Content."""
    return {
        "choices": [
            {
                "message": {
                    "content": json.dumps(data, ensure_ascii=False)
                }
            }
        ],
        "usage": {
            "prompt_tokens": tokens // 2,
            "completion_tokens": tokens // 2,
            "total_tokens": tokens,
        },
    }


SIMPLE_SCHEMA = {
    "type": "object",
    "properties": {
        "vendor": {"type": "string"},
        "total": {"type": "string"},
    },
}

INVOICE_MARKDOWN = """
# Rechnung 2024-001

**Lieferant:** ACME GmbH
**Rechnungsnummer:** RE-2024-001
**Datum:** 15.03.2024
**Nettobetrag:** 42,00 EUR
**MwSt. 19%:** 7,98 EUR
**Gesamtbetrag:** 49,98 EUR
"""

LETTER_MARKDOWN = """
# Sehr geehrte Damen und Herren,

hiermit bestätigen wir Ihre Anmeldung zum Kurs "Grundlagen der Mathematik".
Der Kurs beginnt am 01.04.2024.

Mit freundlichen Grüßen
"""


# ---------------------------------------------------------------------------
# Tests: _meta Block ist im extracted JSON enthalten
# ---------------------------------------------------------------------------


class TestMetaBlockInExtracted:
    """_meta Block wird bei jeder Extraktion automatisch zurückgegeben."""

    def test_meta_block_in_extracted(self):
        """_meta Block ist im extracted JSON vorhanden."""
        response_data = {
            "vendor": "ACME GmbH",
            "total": "49.98",
            "_meta": {
                "steuerrelevant": True,
                "steuerrelevanz_hinweis": "MwSt. 19%: 7,98 EUR",
                "steuer_kategorie": None,
                "steuerjahr": "2024",
                "mwst_ausgewiesen": True,
                "mwst_betrag": "7.98",
                "mwst_satz": "19",
                "aktenzeichen": None,
                "dokumenten_id": "RE-2024-001",
            },
        }
        api_response = _make_mistral_response(response_data)

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "call_mistral_vision_api", new=AsyncMock(return_value=api_response)):
            result = run_async(extract_structured_data(INVOICE_MARKDOWN, SIMPLE_SCHEMA))

        assert result["success"] is True
        assert "_meta" in result["extracted"]

    def test_meta_block_steuerrelevant(self):
        """steuerrelevant=true bei Rechnung mit MwSt."""
        response_data = {
            "vendor": "ACME GmbH",
            "total": "49.98",
            "_meta": {
                "steuerrelevant": True,
                "steuerrelevanz_hinweis": "MwSt. 19%",
                "steuer_kategorie": None,
                "steuerjahr": "2024",
                "mwst_ausgewiesen": True,
                "mwst_betrag": "7.98",
                "mwst_satz": "19",
                "aktenzeichen": None,
                "dokumenten_id": "RE-2024-001",
            },
        }
        api_response = _make_mistral_response(response_data)

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "call_mistral_vision_api", new=AsyncMock(return_value=api_response)):
            result = run_async(extract_structured_data(INVOICE_MARKDOWN, SIMPLE_SCHEMA))

        assert result["success"] is True
        meta = result["extracted"]["_meta"]
        assert meta["steuerrelevant"] is True
        assert meta["mwst_ausgewiesen"] is True

    def test_meta_block_not_steuerrelevant(self):
        """steuerrelevant=false bei einem einfachen Brief."""
        response_data = {
            "vendor": None,
            "total": None,
            "_meta": {
                "steuerrelevant": False,
                "steuerrelevanz_hinweis": None,
                "steuer_kategorie": None,
                "steuerjahr": None,
                "mwst_ausgewiesen": False,
                "mwst_betrag": None,
                "mwst_satz": None,
                "aktenzeichen": None,
                "dokumenten_id": None,
            },
        }
        api_response = _make_mistral_response(response_data)

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "call_mistral_vision_api", new=AsyncMock(return_value=api_response)):
            result = run_async(extract_structured_data(LETTER_MARKDOWN, SIMPLE_SCHEMA))

        assert result["success"] is True
        meta = result["extracted"]["_meta"]
        assert meta["steuerrelevant"] is False
        assert meta["mwst_ausgewiesen"] is False

    def test_meta_block_mwst(self):
        """mwst_ausgewiesen, mwst_betrag, mwst_satz werden korrekt extrahiert."""
        response_data = {
            "vendor": "ACME GmbH",
            "total": "49.98",
            "_meta": {
                "steuerrelevant": True,
                "steuerrelevanz_hinweis": "MwSt. 19%: 7,98 EUR",
                "steuer_kategorie": None,
                "steuerjahr": "2024",
                "mwst_ausgewiesen": True,
                "mwst_betrag": "7.98",
                "mwst_satz": "19",
                "aktenzeichen": None,
                "dokumenten_id": "RE-2024-001",
            },
        }
        api_response = _make_mistral_response(response_data)

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "call_mistral_vision_api", new=AsyncMock(return_value=api_response)):
            result = run_async(extract_structured_data(INVOICE_MARKDOWN, SIMPLE_SCHEMA))

        assert result["success"] is True
        meta = result["extracted"]["_meta"]
        assert meta["mwst_ausgewiesen"] is True
        assert meta["mwst_betrag"] == "7.98"
        assert meta["mwst_satz"] == "19"

    def test_meta_block_dokumenten_id(self):
        """dokumenten_id wird aus dem Dokument extrahiert."""
        response_data = {
            "vendor": "ACME GmbH",
            "total": "49.98",
            "_meta": {
                "steuerrelevant": True,
                "steuerrelevanz_hinweis": None,
                "steuer_kategorie": None,
                "steuerjahr": "2024",
                "mwst_ausgewiesen": True,
                "mwst_betrag": "7.98",
                "mwst_satz": "19",
                "aktenzeichen": None,
                "dokumenten_id": "RE-2024-001",
            },
        }
        api_response = _make_mistral_response(response_data)

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "call_mistral_vision_api", new=AsyncMock(return_value=api_response)):
            result = run_async(extract_structured_data(INVOICE_MARKDOWN, SIMPLE_SCHEMA))

        assert result["success"] is True
        assert result["extracted"]["_meta"]["dokumenten_id"] == "RE-2024-001"


# ---------------------------------------------------------------------------
# Tests: Datentyp-Konventionen
# ---------------------------------------------------------------------------


class TestDatentypKonventionen:
    """Datentyp-Konventionen werden korrekt angewendet."""

    def test_datentyp_datum_iso(self):
        """Datum wird im ISO-Format YYYY-MM-DD zurückgegeben."""
        response_data = {
            "vendor": "ACME GmbH",
            "total": "49.98",
            "date": "2024-03-15",
            "_meta": {
                "steuerrelevant": True,
                "steuerrelevanz_hinweis": None,
                "steuer_kategorie": None,
                "steuerjahr": "2024",
                "mwst_ausgewiesen": True,
                "mwst_betrag": "7.98",
                "mwst_satz": "19",
                "aktenzeichen": None,
                "dokumenten_id": "RE-2024-001",
            },
        }
        api_response = _make_mistral_response(response_data)

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "call_mistral_vision_api", new=AsyncMock(return_value=api_response)):
            result = run_async(extract_structured_data(INVOICE_MARKDOWN, SIMPLE_SCHEMA))

        assert result["success"] is True
        # ISO-Format: YYYY-MM-DD
        date_val = result["extracted"]["date"]
        assert date_val == "2024-03-15"
        parts = date_val.split("-")
        assert len(parts) == 3
        assert len(parts[0]) == 4  # Jahr vierstellig
        assert len(parts[1]) == 2  # Monat zweistellig
        assert len(parts[2]) == 2  # Tag zweistellig

    def test_datentyp_betrag_string(self):
        """Betrag wird als String mit 2 Nachkommastellen zurückgegeben."""
        response_data = {
            "vendor": "ACME GmbH",
            "total": "49.98",
            "_meta": {
                "steuerrelevant": True,
                "steuerrelevanz_hinweis": None,
                "steuer_kategorie": None,
                "steuerjahr": "2024",
                "mwst_ausgewiesen": True,
                "mwst_betrag": "7.98",
                "mwst_satz": "19",
                "aktenzeichen": None,
                "dokumenten_id": None,
            },
        }
        api_response = _make_mistral_response(response_data)

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "call_mistral_vision_api", new=AsyncMock(return_value=api_response)):
            result = run_async(extract_structured_data(INVOICE_MARKDOWN, SIMPLE_SCHEMA))

        assert result["success"] is True
        # Betrag als String mit 2 Nachkommastellen
        mwst_betrag = result["extracted"]["_meta"]["mwst_betrag"]
        assert isinstance(mwst_betrag, str)
        assert "." in mwst_betrag
        assert len(mwst_betrag.split(".")[1]) == 2

    def test_datentyp_null_statt_leer(self):
        """Fehlende Werte werden als null (None) und nicht als leerer String zurückgegeben."""
        response_data = {
            "vendor": None,
            "total": None,
            "_meta": {
                "steuerrelevant": False,
                "steuerrelevanz_hinweis": None,
                "steuer_kategorie": None,
                "steuerjahr": None,
                "mwst_ausgewiesen": False,
                "mwst_betrag": None,
                "mwst_satz": None,
                "aktenzeichen": None,
                "dokumenten_id": None,
            },
        }
        api_response = _make_mistral_response(response_data)

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "call_mistral_vision_api", new=AsyncMock(return_value=api_response)):
            result = run_async(extract_structured_data(LETTER_MARKDOWN, SIMPLE_SCHEMA))

        assert result["success"] is True
        meta = result["extracted"]["_meta"]
        # Alle fehlenden Werte müssen None sein, keine leeren Strings
        for field in ["steuerrelevanz_hinweis", "steuer_kategorie", "steuerjahr",
                      "mwst_betrag", "mwst_satz", "aktenzeichen", "dokumenten_id"]:
            assert meta[field] is None, f"Feld '{field}' sollte None sein, ist aber: {meta[field]!r}"
            assert meta[field] != "", f"Feld '{field}' darf kein leerer String sein"


# ---------------------------------------------------------------------------
# Tests: _meta Block im Prompt enthalten
# ---------------------------------------------------------------------------


class TestMetaBlockInPrompt:
    """_meta Block Instruktion wird in den API-Prompt eingebettet."""

    def test_meta_schema_in_prompt_de(self):
        """_META_SCHEMA Felder sind im deutschen Prompt enthalten."""
        response_data = {"vendor": "Test", "_meta": {"steuerrelevant": False, "mwst_ausgewiesen": False,
                                                       "steuerrelevanz_hinweis": None, "steuer_kategorie": None,
                                                       "steuerjahr": None, "mwst_betrag": None, "mwst_satz": None,
                                                       "aktenzeichen": None, "dokumenten_id": None}}
        api_response = _make_mistral_response(response_data)

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "call_mistral_vision_api",
                          new=AsyncMock(return_value=api_response)) as mock_api:
            run_async(extract_structured_data(LETTER_MARKDOWN, SIMPLE_SCHEMA, language="de"))

        call_payload = mock_api.call_args[0][0]
        user_content = call_payload["messages"][1]["content"]
        # _meta Block Felder müssen im Prompt vorhanden sein
        assert "steuerrelevant" in user_content
        assert "mwst_ausgewiesen" in user_content
        assert "dokumenten_id" in user_content
        assert "_meta" in user_content

    def test_meta_schema_in_prompt_en(self):
        """_META_SCHEMA Felder sind im englischen Prompt enthalten."""
        response_data = {"vendor": "Test", "_meta": {"steuerrelevant": False, "mwst_ausgewiesen": False,
                                                       "steuerrelevanz_hinweis": None, "steuer_kategorie": None,
                                                       "steuerjahr": None, "mwst_betrag": None, "mwst_satz": None,
                                                       "aktenzeichen": None, "dokumenten_id": None}}
        api_response = _make_mistral_response(response_data)

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "call_mistral_vision_api",
                          new=AsyncMock(return_value=api_response)) as mock_api:
            run_async(extract_structured_data(LETTER_MARKDOWN, SIMPLE_SCHEMA, language="en"))

        call_payload = mock_api.call_args[0][0]
        user_content = call_payload["messages"][1]["content"]
        # _meta Block muss auch im englischen Prompt vorhanden sein
        assert "steuerrelevant" in user_content
        assert "_meta" in user_content

    def test_datentyp_konventionen_in_prompt(self):
        """Datentyp-Konventionen (ISO 8601, Decimal) sind im Prompt enthalten."""
        response_data = {"vendor": "Test", "_meta": {"steuerrelevant": False, "mwst_ausgewiesen": False,
                                                       "steuerrelevanz_hinweis": None, "steuer_kategorie": None,
                                                       "steuerjahr": None, "mwst_betrag": None, "mwst_satz": None,
                                                       "aktenzeichen": None, "dokumenten_id": None}}
        api_response = _make_mistral_response(response_data)

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "call_mistral_vision_api",
                          new=AsyncMock(return_value=api_response)) as mock_api:
            run_async(extract_structured_data(INVOICE_MARKDOWN, SIMPLE_SCHEMA, language="de"))

        call_payload = mock_api.call_args[0][0]
        user_content = call_payload["messages"][1]["content"]
        # Datentyp-Konventionen müssen im Prompt vorhanden sein
        assert "ISO 8601" in user_content
        assert "null" in user_content

    def test_signalwoerter_in_prompt(self):
        """Steuerrelevanz-Signalwörter sind im Prompt enthalten."""
        response_data = {"vendor": "Test", "_meta": {"steuerrelevant": False, "mwst_ausgewiesen": False,
                                                       "steuerrelevanz_hinweis": None, "steuer_kategorie": None,
                                                       "steuerjahr": None, "mwst_betrag": None, "mwst_satz": None,
                                                       "aktenzeichen": None, "dokumenten_id": None}}
        api_response = _make_mistral_response(response_data)

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "call_mistral_vision_api",
                          new=AsyncMock(return_value=api_response)) as mock_api:
            run_async(extract_structured_data(INVOICE_MARKDOWN, SIMPLE_SCHEMA, language="de"))

        call_payload = mock_api.call_args[0][0]
        user_content = call_payload["messages"][1]["content"]
        # Mindestens ein Signalwort muss im Prompt stehen
        assert "Finanzamt" in user_content or "Werbungskosten" in user_content
