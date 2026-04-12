"""
Tests für T-MKIT-014: Schema-basierte strukturierte Extraktion.

Alle Tests laufen ohne Docker-Container und ohne echte API-Calls.
Alle externen Abhängigkeiten werden per unittest.mock gemockt.
"""

import json
import re
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import psycopg2
import pytest

from conftest import load_server_module, read_text_fixture, run_async


_DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://daigestr:daigestr@localhost:15432/daigestr",
)

# Einmal laden; alle Tests in diesem Modul teilen diese Instanz
_server = load_server_module(use_real_pil=False)
extract_structured_data = _server.extract_structured_data
get_template_by_id = _server.get_template_by_id
get_all_template_ids = _server.get_all_template_ids

# DB initialisieren (echte PostgreSQL, tables + seed)
import templates_db as _tdb
import intelligence as _intelligence
_tdb.pool_reset()
conn = psycopg2.connect(_DB_URL)
conn.autocommit = True
cur = conn.cursor()
cur.execute("TRUNCATE TABLE template, prompt, scoring_weight, cache RESTART IDENTITY CASCADE")
conn.close()
_tdb.pool_reset()
_server.init_templates_db()


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def _make_mistral_json_response(data: dict, tokens: int = 20) -> dict:
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


INVOICE_SCHEMA = {
    "type": "object",
    "properties": {
        "invoice_number": {"type": "string"},
        "total_amount": {"type": "number"},
        "vendor": {"type": "string"},
    },
}

INVOICE_MARKDOWN = """
# Rechnung 2024-001

**Lieferant:** ACME GmbH
**Rechnungsnummer:** 2024-001
**Gesamtbetrag:** 1.234,56 EUR
"""


# ---------------------------------------------------------------------------
# Tests: extract_structured_data()
# ---------------------------------------------------------------------------


class TestExtractStructuredData:
    """Tests für die extract_structured_data() Funktion."""

    def test_extract_structured_data_success(self):
        """AC-014-2/3: Erfolgreiche Extraktion gibt extracted-Dict zurück."""
        expected = {"invoice_number": "2024-001", "total_amount": 1234.56, "vendor": "ACME GmbH"}
        api_response = _make_mistral_json_response(expected, tokens=30)

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "call_mistral_vision_api", new=AsyncMock(return_value=api_response)):
            result = run_async(extract_structured_data(INVOICE_MARKDOWN, INVOICE_SCHEMA))

        assert result["success"] is True
        assert result["extracted"] == expected
        assert result["tokens"] == 30

    def test_extract_with_template_invoice(self):
        """AC-014-4: Invoice-Template enthält alle erwarteten Felder."""
        tmpl = get_template_by_id("invoice")
        assert tmpl is not None
        schema = tmpl["schema"]

        # Seed-Schema nutzt deutsche Feldnamen
        assert "rechnungsnummer" in schema["properties"]
        assert "positionen" in schema["properties"]
        assert "netto" in schema["properties"]
        assert "brutto" in schema["properties"]
        assert "datum" in schema["properties"]

    def test_extract_with_template_cv(self):
        """AC-014-4: CV-Template enthält alle erwarteten Felder."""
        tmpl = get_template_by_id("cv")
        assert tmpl is not None
        schema = tmpl["schema"]

        assert "name" in schema["properties"]
        assert "email" in schema["properties"]
        # Seed-Schema nutzt deutsche Feldnamen für Skills/Experience/Education
        assert "kenntnisse" in schema["properties"]
        assert "berufserfahrung" in schema["properties"]
        assert "ausbildung" in schema["properties"]

    def test_extract_with_template_contract(self):
        """AC-014-4: Contract-Template enthält alle erwarteten Felder."""
        tmpl = get_template_by_id("contract")
        assert tmpl is not None
        schema = tmpl["schema"]

        # Seed-Schema nutzt deutsche Feldnamen
        assert "vertragsparteien" in schema["properties"]
        assert "gegenstand" in schema["properties"]
        assert "laufzeit" in schema["properties"]

    def test_extract_with_template_bank_statement_supports_bundles(self):
        """bank_statement must support multi-statement PDFs via kontoauszuege + zeitraum."""
        tmpl = get_template_by_id("bank_statement")
        assert tmpl is not None
        schema = tmpl["schema"]

        assert "zeitraum" in schema["properties"]
        assert "währung" in schema["properties"]
        assert "kontoauszuege" in schema["properties"]
        statement_item = schema["properties"]["kontoauszuege"]["items"]
        assert "auszugsnummer" in statement_item["properties"]
        assert "buchungen" in statement_item["properties"]
        assert "zeitraum" in statement_item["properties"]

    def test_extract_invalid_json_response(self):
        """Graceful Handling bei ungültigem JSON in der API-Antwort."""
        bad_response = {
            "choices": [
                {
                    "message": {
                        "content": "Das ist definitiv kein JSON und enthält keine geschweiften Klammern."
                    }
                }
            ],
            "usage": {"total_tokens": 5},
        }

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "call_mistral_vision_api", new=AsyncMock(return_value=bad_response)):
            result = run_async(extract_structured_data(INVOICE_MARKDOWN, INVOICE_SCHEMA))

        assert result["success"] is False
        assert result["extracted"] is None
        assert "error" in result

    def test_extract_validates_against_schema(self):
        """AC-014-7: Schema-Validierung wird durchgeführt (kein Fehler bei valider Antwort)."""
        valid_data = {"invoice_number": "INV-001", "total_amount": 500.0, "vendor": "Test GmbH"}
        api_response = _make_mistral_json_response(valid_data)

        # jsonschema im server-Modul patchen um Validierung zu testen
        mock_jsonschema = MagicMock()
        mock_jsonschema.validate = MagicMock()  # kein Fehler → valide
        mock_jsonschema.ValidationError = Exception

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "call_mistral_vision_api", new=AsyncMock(return_value=api_response)), \
             patch.object(_server, "JSONSCHEMA_AVAILABLE", True), \
             patch.dict(sys.modules, {"jsonschema": mock_jsonschema}):
            # Direkt jsonschema im server-Modul patchen
            original_jsonschema = getattr(_server, "jsonschema", None)
            _server.jsonschema = mock_jsonschema
            try:
                result = run_async(extract_structured_data(INVOICE_MARKDOWN, INVOICE_SCHEMA))
            finally:
                if original_jsonschema is not None:
                    _server.jsonschema = original_jsonschema
                elif hasattr(_server, "jsonschema"):
                    del _server.jsonschema

        assert result["success"] is True
        assert result["extracted"] == valid_data

    def test_extract_api_error(self):
        """Graceful Handling bei API-Fehler (Exception)."""
        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "call_mistral_vision_api",
                          new=AsyncMock(side_effect=Exception("Connection refused"))):
            result = run_async(extract_structured_data(INVOICE_MARKDOWN, INVOICE_SCHEMA))

        assert result["success"] is False
        assert result["extracted"] is None
        assert "API-Fehler" in result.get("error", "") or result.get("error")

    def test_extract_default_off_no_api_key(self):
        """AC-014-2: Ohne MISTRAL_API_KEY wird kein API-Call durchgeführt."""
        with patch.object(_server, "MISTRAL_API_KEY", ""), \
             patch.object(_server, "call_mistral_vision_api", new=AsyncMock()) as mock_api:
            result = run_async(extract_structured_data(INVOICE_MARKDOWN, INVOICE_SCHEMA))
            mock_api.assert_not_called()

        assert result["success"] is False
        assert result["extracted"] is None

    def test_extract_schema_sent_in_prompt(self):
        """AC-014-2: Das Schema wird im Prompt an die API übermittelt."""
        valid_data = {"invoice_number": "X1"}
        api_response = _make_mistral_json_response(valid_data)

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "call_mistral_vision_api",
                          new=AsyncMock(return_value=api_response)) as mock_api:
            run_async(extract_structured_data(INVOICE_MARKDOWN, INVOICE_SCHEMA))

        call_payload = mock_api.call_args[0][0]
        user_content = call_payload["messages"][1]["content"]
        # Schema muss im Prompt enthalten sein
        assert "invoice_number" in user_content
        assert "JSON" in user_content or "json" in user_content.lower()

    def test_extract_json_in_markdown_codeblock(self):
        """JSON das in Markdown-Code-Block eingebettet ist, wird korrekt extrahiert."""
        wrapped_content = '```json\n{"invoice_number": "B-2024", "total_amount": 99.0}\n```'
        wrapped_response = {
            "choices": [{"message": {"content": wrapped_content}}],
            "usage": {"total_tokens": 15},
        }

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "call_mistral_vision_api", new=AsyncMock(return_value=wrapped_response)):
            result = run_async(extract_structured_data(INVOICE_MARKDOWN, INVOICE_SCHEMA))

        assert result["success"] is True
        assert result["extracted"]["invoice_number"] == "B-2024"

    def test_extract_tokens_returned(self):
        """Token-Verbrauch wird korrekt zurückgegeben."""
        api_response = _make_mistral_json_response({"vendor": "Test"}, tokens=42)

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "call_mistral_vision_api", new=AsyncMock(return_value=api_response)):
            result = run_async(extract_structured_data(INVOICE_MARKDOWN, INVOICE_SCHEMA))

        assert result["tokens"] == 42

    def test_extract_language_en_uses_english_prompt(self):
        """Bei language='en' wird ein englischer Prompt verwendet."""
        api_response = _make_mistral_json_response({"vendor": "Test Corp"})

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "call_mistral_vision_api",
                          new=AsyncMock(return_value=api_response)) as mock_api:
            run_async(extract_structured_data(INVOICE_MARKDOWN, INVOICE_SCHEMA, language="en"))

        call_payload = mock_api.call_args[0][0]
        user_content = call_payload["messages"][1]["content"]
        # Englischer User-Prompt enthält "JSON schema" oder "Extract"
        assert "Extract" in user_content or "schema" in user_content.lower()

    def test_extract_harmonizes_bank_statement_bundle_output(self):
        """Multi-statement bank PDFs must be aggregated without losing per-statement detail."""
        bundle = {
            "bank": "Stadtsparkasse Mönchengladbach",
            "iban": "DE62310500000000019752",
            "bic": "MGLSDE33",
            "kontoinhaber": {"name": "Hans und Marlene Kuhlen"},
            "_meta": {
                "steuerrelevant": True,
                "mwst_ausgewiesen": False,
                "zusammenfassung": "Veraltete Einzelauszug-Zusammenfassung",
                "dokumenten_id": "11",
            },
            "kontoauszuege": [
                {
                    "auszugsnummer": "11",
                    "datum": "2024-08-30",
                    "anfangssaldo": "14902.01",
                    "endsaldo": "15504.16",
                    "buchungen": [
                        {"datum": "2024-08-19", "text": "A", "betrag": "-41.90", "saldo": "14860.11", "währung": "EUR"},
                        {"datum": "2024-08-30", "text": "B", "betrag": "+766.09", "saldo": "15504.16", "währung": "EUR"},
                    ],
                },
                {
                    "auszugsnummer": "12",
                    "datum": "2024-09-30",
                    "anfangssaldo": "15504.16",
                    "endsaldo": "12235.38",
                    "buchungen": [
                        {"datum": "2024-09-02", "text": "C", "betrag": "-27.85", "saldo": "12235.38", "währung": "EUR"},
                    ],
                },
            ],
        }
        api_response = _make_mistral_json_response(bundle, tokens=42)

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "call_mistral_vision_api", new=AsyncMock(return_value=api_response)):
            result = run_async(extract_structured_data("dummy markdown", get_template_by_id("bank_statement")["schema"]))

        assert result["success"] is True
        extracted = result["extracted"]
        assert extracted["auszugsnummer"] == "11,12"
        assert extracted["datum"] == "2024-09-30"
        assert extracted["anfangssaldo"] == "14902.01"
        assert extracted["endsaldo"] == "12235.38"
        assert extracted["währung"] == "EUR"
        assert extracted["zeitraum"] == {"von": "2024-08-19", "bis": "2024-09-02"}
        assert len(extracted["kontoauszuege"]) == 2
        assert len(extracted["buchungen"]) == 3
        assert extracted["buchungen"][0]["datum"] == "2024-08-19"
        assert extracted["buchungen"][-1]["datum"] == "2024-09-02"
        assert extracted["_meta"]["dokumenten_id"] == "11,12"
        assert extracted["summary"].startswith("Sammel-Kontoauszug")

    def test_extract_recovers_bank_statement_bundle_from_markdown_segments(self):
        """If the first extraction collapses a bundle to one statement, Daigestr must re-extract per statement."""
        markdown = """
## Seite 1
Kontoauszug 11
Blatt 1
19.08 Lastschrift
Wert: 19.08.2024

## Seite 2
Kontoauszug 11
Blatt 2
Kontostand am 30.08.2024, 20:04 Uhr 15.504,16+

## Seite 3
Kontoauszug 12
Blatt 1
02.09 Lastschrift
Wert: 02.09.2024

## Seite 4
Kontoauszug 12
Blatt 2
Kontostand am 30.09.2024, 20:03 Uhr 12.263,23+
"""
        collapsed = {
            "bank": "Stadtsparkasse Mönchengladbach",
            "iban": "DE62310500000000019752",
            "bic": "MGLSDE33",
            "kontoinhaber": {"name": "Hans und Marlene Kuhlen"},
            "auszugsnummer": "11",
            "datum": "2024-08-30",
            "anfangssaldo": "14902.01",
            "endsaldo": "15504.16",
            "buchungen": [
                {"datum": "2024-08-19", "text": "A", "betrag": "-41.90", "saldo": "14860.11", "währung": "EUR"},
                {"datum": "2024-08-30", "text": "B", "betrag": "+766.09", "saldo": "15504.16", "währung": "EUR"},
            ],
            "_meta": {"zusammenfassung": "Einzel-Auszug 11", "dokumenten_id": "11"},
        }
        statement_11 = {
            "bank": "Stadtsparkasse Mönchengladbach",
            "iban": "DE62310500000000019752",
            "bic": "MGLSDE33",
            "kontoinhaber": {"name": "Hans und Marlene Kuhlen"},
            "auszugsnummer": "11",
            "datum": "2024-08-30",
            "anfangssaldo": "14902.01",
            "endsaldo": "15504.16",
            "buchungen": [
                {"datum": "2024-08-19", "text": "A", "betrag": "-41.90", "saldo": "14860.11", "währung": "EUR"},
                {"datum": "2024-08-30", "text": "B", "betrag": "+766.09", "saldo": "15504.16", "währung": "EUR"},
            ],
        }
        statement_12 = {
            "bank": "Stadtsparkasse Mönchengladbach",
            "iban": "DE62310500000000019752",
            "bic": "MGLSDE33",
            "kontoinhaber": {"name": "Hans und Marlene Kuhlen"},
            "auszugsnummer": "12",
            "datum": "2024-09-30",
            "anfangssaldo": "15504.16",
            "endsaldo": "12263.23",
            "buchungen": [
                {"datum": "2024-09-02", "text": "C", "betrag": "-27.85", "saldo": "15476.31", "währung": "EUR"},
                {"datum": "2024-09-30", "text": "D", "betrag": "+766.09", "saldo": "12263.23", "währung": "EUR"},
            ],
        }

        api_calls = AsyncMock(side_effect=[
            _make_mistral_json_response(collapsed, tokens=50),
            _make_mistral_json_response(statement_11, tokens=30),
            _make_mistral_json_response(statement_12, tokens=32),
        ])

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "call_mistral_vision_api", new=api_calls):
            result = run_async(extract_structured_data(markdown, get_template_by_id("bank_statement")["schema"]))

        assert result["success"] is True
        extracted = result["extracted"]
        assert api_calls.await_count == 3
        assert extracted["auszugsnummer"] == "11,12"
        assert extracted["zeitraum"] == {"von": "2024-08-19", "bis": "2024-09-30"}
        assert len(extracted["kontoauszuege"]) == 2
        assert len(extracted["buchungen"]) == 4
        assert extracted["buchungen"][0]["datum"] == "2024-08-19"
        assert extracted["buchungen"][-1]["datum"] == "2024-09-30"
        assert extracted["_meta"]["dokumenten_id"] == "11,12"
        assert extracted["summary"].startswith("Sammel-Kontoauszug")

    def test_extract_recovers_bank_statement_bundle_after_initial_api_failure(self):
        """Long bank-statement runs must fall back to segment extraction when the first full extract call fails."""
        markdown = """
## Seite 1
Kontoauszug 11
Blatt 1
19.08 Lastschrift
Wert: 19.08.2024

## Seite 2
Kontoauszug 11
Blatt 2
Kontostand am 30.08.2024, 20:04 Uhr 15.504,16+

## Seite 3
Kontoauszug 12
Blatt 1
02.09 Lastschrift
Wert: 02.09.2024

## Seite 4
Kontoauszug 12
Blatt 2
Kontostand am 30.09.2024, 20:03 Uhr 12.263,23+
"""
        statement_11 = {
            "bank": "Stadtsparkasse Mönchengladbach",
            "iban": "DE62310500000000019752",
            "bic": "MGLSDE33",
            "kontoinhaber": {"name": "Hans und Marlene Kuhlen"},
            "auszugsnummer": "11",
            "datum": "2024-08-30",
            "anfangssaldo": "14902.01",
            "endsaldo": "15504.16",
            "buchungen": [
                {"datum": "2024-08-19", "text": "A", "betrag": "-41.90", "saldo": "14860.11", "währung": "EUR"},
                {"datum": "2024-08-30", "text": "B", "betrag": "+766.09", "saldo": "15504.16", "währung": "EUR"},
            ],
        }
        statement_12 = {
            "bank": "Stadtsparkasse Mönchengladbach",
            "iban": "DE62310500000000019752",
            "bic": "MGLSDE33",
            "kontoinhaber": {"name": "Hans und Marlene Kuhlen"},
            "auszugsnummer": "12",
            "datum": "2024-09-30",
            "anfangssaldo": "15504.16",
            "endsaldo": "12263.23",
            "buchungen": [
                {"datum": "2024-09-02", "text": "C", "betrag": "-27.85", "saldo": "15476.31", "währung": "EUR"},
                {"datum": "2024-09-30", "text": "D", "betrag": "+766.09", "saldo": "12263.23", "währung": "EUR"},
            ],
        }

        api_calls = AsyncMock(side_effect=[
            Exception("upstream timeout"),
            _make_mistral_json_response(statement_11, tokens=30),
            _make_mistral_json_response(statement_12, tokens=32),
        ])

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "call_mistral_vision_api", new=api_calls):
            result = run_async(extract_structured_data(markdown, get_template_by_id("bank_statement")["schema"]))

        assert result["success"] is True
        extracted = result["extracted"]
        assert api_calls.await_count == 3
        assert extracted["auszugsnummer"] == "11,12"
        assert extracted["zeitraum"] == {"von": "2024-08-19", "bis": "2024-09-30"}
        assert len(extracted["kontoauszuege"]) == 2
        assert len(extracted["buchungen"]) == 4

    def test_extract_bank_statement_page_groups_from_realistic_sanitized_fixture(self):
        """A sanitized 12421-derived fixture must still split into the original statement groups."""
        markdown = read_text_fixture("bank_statement_bundle_12421_sanitized.md")

        page_groups = _intelligence._extract_bank_statement_page_groups(markdown)

        assert [number for number, _ in page_groups] == ["11", "12", "13", "14", "15", "16", "17", "18"]
        assert len(page_groups) == 8
        assert "Kontoauszug 11" in page_groups[0][1]
        assert "Wert: 19.08.2024" in page_groups[0][1]
        assert "Kontoauszug 18" in page_groups[-1][1]
        assert "Wert: 03.03.2025" in page_groups[-1][1]

    def test_extract_recovers_bank_statement_bundle_from_realistic_sanitized_fixture(self):
        """The sanitized 12421 regression fixture must replay bundle recovery without live OCR/LLM calls."""
        markdown = read_text_fixture("bank_statement_bundle_12421_sanitized.md")
        schema = get_template_by_id("bank_statement")["schema"]

        async def fake_extract(statement_markdown, passed_schema, **kwargs):
            assert passed_schema == schema
            match = re.search(r"Kontoauszug\s+(\d+)", statement_markdown)
            assert match is not None
            statement_number = match.group(1)
            booking_dates = re.findall(r"Wert:\s*(\d{2}\.\d{2}\.\d{4})", statement_markdown)
            assert booking_dates, f"fixture segment for statement {statement_number} has no booking dates"
            first_date = booking_dates[0]
            last_date = booking_dates[-1]
            return {
                "success": True,
                "tokens": 10,
                "extracted": {
                    "bank": "Bankhaus Beispielstadt",
                    "iban": "DE02100100100000019752",
                    "bic": "TESTDEFFXXX",
                    "kontoinhaber": {"name": "Konto Muster"},
                    "auszugsnummer": statement_number,
                    "datum": last_date[-4:] + "-" + last_date[3:5] + "-" + last_date[0:2],
                    "anfangssaldo": "1000.00",
                    "endsaldo": "1100.00",
                    "buchungen": [
                        {
                            "datum": first_date[-4:] + "-" + first_date[3:5] + "-" + first_date[0:2],
                            "text": f"Segment {statement_number}",
                            "betrag": "-10.00",
                            "saldo": "990.00",
                            "währung": "EUR",
                        },
                        {
                            "datum": last_date[-4:] + "-" + last_date[3:5] + "-" + last_date[0:2],
                            "text": f"Abschluss {statement_number}",
                            "betrag": "+110.00",
                            "saldo": "1100.00",
                            "währung": "EUR",
                        },
                    ],
                    "_meta": {
                        "dokumenten_id": statement_number,
                        "zusammenfassung": f"Auszug {statement_number}",
                    },
                },
            }

        with patch.object(_intelligence, "extract_structured_data", new=AsyncMock(side_effect=fake_extract)):
            result = run_async(
                _intelligence._recover_bank_statement_bundle(
                    markdown,
                    schema,
                    language="de",
                    field_descriptions=None,
                    notes=None,
                    request_id="fixture-12421",
                    attempt_number=2,
                )
            )

        assert result is not None
        assert result["success"] is True
        assert result["tokens"] == 80
        extracted = result["extracted"]
        assert extracted["auszugsnummer"] == "11,12,13,14,15,16,17,18"
        assert extracted["zeitraum"] == {"von": "2024-08-19", "bis": "2025-03-17"}
        assert len(extracted["kontoauszuege"]) == 8
        assert len(extracted["buchungen"]) == 16
        assert extracted["_meta"]["dokumenten_id"] == "11,12,13,14,15,16,17,18"
        assert extracted["summary"].startswith("Sammel-Kontoauszug")
        assert "mit 8 Auszügen" in extracted["summary"]


# ---------------------------------------------------------------------------
# Tests: EXTRACTION_TEMPLATES
# ---------------------------------------------------------------------------


class TestExtractionTemplates:
    """Tests für die vordefinierten Extraktions-Templates (aus DB / seed.sql)."""

    def test_templates_all_three_exist(self):
        """AC-014-4: Alle drei Seed-Templates existieren in der DB."""
        ids = get_all_template_ids()
        assert "invoice" in ids
        assert "cv" in ids
        assert "contract" in ids

    def test_templates_are_valid_json_schemas(self):
        """Alle Templates haben eine gültige JSON-Schema-Struktur (type + properties)."""
        for tmpl_id in get_all_template_ids():
            tmpl = get_template_by_id(tmpl_id)
            schema = tmpl["schema"]
            assert "type" in schema, f"Template '{tmpl_id}' fehlt 'type'"
            assert schema["type"] == "object", f"Template '{tmpl_id}' sollte type='object' haben"
            assert "properties" in schema, f"Template '{tmpl_id}' fehlt 'properties'"
            assert isinstance(schema["properties"], dict), f"Template '{tmpl_id}' properties sollte dict sein"

    def test_invoice_template_line_items_is_array(self):
        """Invoice-Template: positionen ist ein Array (Seed nutzt deutsche Feldnamen)."""
        tmpl = get_template_by_id("invoice")
        positionen = tmpl["schema"]["properties"]["positionen"]
        assert positionen["type"] == "array"
        assert "items" in positionen

    def test_cv_template_skills_is_array_of_strings(self):
        """CV-Template: sprachen ist ein Array von Strings (Seed nutzt deutsche Feldnamen)."""
        tmpl = get_template_by_id("cv")
        sprachen = tmpl["schema"]["properties"]["sprachen"]
        assert sprachen["type"] == "array"

    def test_contract_template_parties_is_array(self):
        """Contract-Template: vertragsparteien ist ein Array (Seed nutzt deutsche Feldnamen)."""
        tmpl = get_template_by_id("contract")
        vertragsparteien = tmpl["schema"]["properties"]["vertragsparteien"]
        assert vertragsparteien["type"] == "array"


# ---------------------------------------------------------------------------
# Tests: /v1/templates Endpoint
# ---------------------------------------------------------------------------


class TestTemplatesEndpoint:
    """Tests für den /v1/templates GET-Endpoint (AC-014-4)."""

    def test_templates_endpoint_returns_all_templates(self):
        """AC-014-4: Seed-Templates sind über get_all_template_ids abrufbar."""
        ids = get_all_template_ids()
        assert "invoice" in ids
        assert "cv" in ids
        assert "contract" in ids

    def test_templates_all_have_valid_schema(self):
        """Jedes Template in der DB hat eine gültige Schema-Struktur."""
        for tmpl_id in get_all_template_ids():
            tmpl = get_template_by_id(tmpl_id)
            assert tmpl is not None
            schema = tmpl["schema"]
            assert "type" in schema
            assert "properties" in schema

    def test_templates_have_display_name(self):
        """Jedes Template hat einen display_name."""
        for tmpl_id in get_all_template_ids():
            tmpl = get_template_by_id(tmpl_id)
            assert tmpl.get("display_name"), f"Template '{tmpl_id}' fehlt display_name"


# ---------------------------------------------------------------------------
# Tests: Integration in convert_auto
# ---------------------------------------------------------------------------


class TestExtractInConvertAuto:
    """Tests für die Integration von extract_schema in convert_auto()."""

    def test_extract_default_off_no_api_call_without_schema(self):
        """AC-014-2: Ohne extract_schema wird extract_structured_data NICHT aufgerufen."""
        with patch.object(_server, "extract_structured_data",
                          new=AsyncMock()) as mock_extract, \
             patch.object(_server, "convert_with_markitdown",
                          return_value={"success": True, "markdown": "# Test"}), \
             patch.object(_server, "detect_mimetype_from_bytes",
                          return_value="text/plain"):
            file_data = b"dummy content for a txt file"
            run_async(_server.convert_auto(
                file_data=file_data,
                filename="test.txt",
                source="test",
                source_type="file",
                input_meta={},
                extract_schema=None,
            ))
            mock_extract.assert_not_called()

    def test_extract_called_when_schema_provided(self):
        """AC-014-2: Mit extract_schema wird extract_structured_data aufgerufen."""
        extracted_data = {"vendor": "TestCo", "total_amount": 100.0}
        with patch.object(_server, "extract_structured_data",
                          new=AsyncMock(return_value={
                              "success": True,
                              "extracted": extracted_data,
                              "tokens": 10,
                          })) as mock_extract, \
             patch.object(_server, "convert_with_markitdown",
                          return_value={"success": True, "markdown": "# Invoice\nVendor: TestCo"}), \
             patch.object(_server, "detect_mimetype_from_bytes",
                          return_value="text/plain"):
            result = run_async(_server.convert_auto(
                file_data=b"dummy txt content",
                filename="invoice.txt",
                source="test",
                source_type="file",
                input_meta={},
                extract_schema=INVOICE_SCHEMA,
            ))

        mock_extract.assert_called_once()
        assert result.extracted == extracted_data

    def test_extract_field_in_response(self):
        """AC-014-3: Response enthält 'extracted' Feld wenn Schema gesetzt."""
        extracted_data = {"invoice_number": "INV-42"}
        with patch.object(_server, "extract_structured_data",
                          new=AsyncMock(return_value={
                              "success": True,
                              "extracted": extracted_data,
                              "tokens": 5,
                          })), \
             patch.object(_server, "convert_with_markitdown",
                          return_value={"success": True, "markdown": "# Invoice"}), \
             patch.object(_server, "detect_mimetype_from_bytes",
                          return_value="text/plain"):
            result = run_async(_server.convert_auto(
                file_data=b"invoice content here",
                filename="invoice.txt",
                source="test",
                source_type="file",
                input_meta={},
                extract_schema=INVOICE_SCHEMA,
            ))

        assert result.success is True
        assert result.extracted is not None
        assert result.extracted["invoice_number"] == "INV-42"

    def test_extract_failure_returns_error(self):
        """Wenn Extraktion fehlschlägt, muss der Convert-Response als Fehler enden."""
        with patch.object(_server, "extract_structured_data",
                          new=AsyncMock(return_value={
                              "success": False,
                              "error": "API nicht erreichbar",
                              "extracted": None,
                              "tokens": 0,
                          })), \
             patch.object(_server, "convert_with_markitdown",
                          return_value={"success": True, "markdown": "# Invoice"}), \
             patch.object(_server, "detect_mimetype_from_bytes",
                          return_value="text/plain"):
            result = run_async(_server.convert_auto(
                file_data=b"invoice content here",
                filename="invoice.txt",
                source="test",
                source_type="file",
                input_meta={},
                extract_schema=INVOICE_SCHEMA,
                no_cache=True,  # Verhindert Cache-Treffer vom vorherigen Test
            ))

        assert result.success is False
        assert result.markdown is not None
        assert result.extracted is None
        assert result.error is not None


class TestStructuredOutputRegression:
    """Vergleicht alte und neue Mistral-Ausgabeformen gegen denselben Contract."""

    def test_extract_structured_data_preserves_output_across_response_formats(self):
        expected = {
            "invoice_number": "INV-2026-04",
            "total_amount": "100.00",
            "_meta": {
                "steuerrelevant": True,
                "mwst_ausgewiesen": False,
                "zusammenfassung": "Rechnung über 100 EUR.",
            },
        }
        schema = {
            "type": "object",
            "properties": {
                "invoice_number": {"type": "string"},
                "total_amount": {"type": "string"},
            },
            "required": ["invoice_number"],
        }

        variants = {
            "legacy": {
                "choices": [{"message": {"content": json.dumps(expected)}}],
                "usage": {"total_tokens": 17},
            },
            "json_object": {
                "choices": [{"message": {"content": expected}}],
                "usage": {"total_tokens": 17},
            },
            "json_schema": {
                "choices": [{"message": {"content": expected}}],
                "usage": {"total_tokens": 17},
            },
        }

        for response_format, api_response in variants.items():
            with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
                 patch.object(_server, "MISTRAL_EXTRACT_RESPONSE_FORMAT", response_format), \
                 patch.object(_server, "call_mistral_vision_api", new=AsyncMock(return_value=api_response)):
                result = run_async(_server.extract_structured_data(INVOICE_MARKDOWN, schema))

            assert result["success"] is True
            assert result["tokens"] == 17
            assert result["extracted"]["invoice_number"] == expected["invoice_number"]
            assert result["extracted"]["total_amount"] == expected["total_amount"]
            assert result["extracted"]["_meta"] == expected["_meta"]
            assert result["extracted"]["summary"] == expected["_meta"]["zusammenfassung"]
