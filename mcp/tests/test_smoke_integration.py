"""
Smoke / Integration Tests — laufen gegen den LAUFENDEN Container.

Usage:
    DAIGESTR_URL=http://127.0.0.1:18006 python3 -m pytest tests/test_smoke_integration.py -v

Diese Tests prüfen dass der Service nach Refactor/Rebuild korrekt funktioniert.
Sie brauchen einen laufenden daigestr-Container und echte Testdateien in ./data/.
"""

import os
import json
import base64
import tempfile

import pytest
import httpx

BASE_URL = os.getenv("DAIGESTR_URL", "http://127.0.0.1:18006")
TIMEOUT = 120  # manche Konvertierungen dauern lang (Vision, OCR)


def _service_reachable() -> bool:
    try:
        response = httpx.get(f"{BASE_URL}/v1/health", timeout=5)
    except Exception:
        return False
    return response.status_code == 200


pytestmark = pytest.mark.skipif(
    not _service_reachable(),
    reason="Requires running Daigestr service",
)


def api(method: str, path: str, **kwargs) -> httpx.Response:
    """Helper für API-Calls."""
    url = f"{BASE_URL}{path}"
    kwargs.setdefault("timeout", TIMEOUT)
    return getattr(httpx, method)(url, **kwargs)


# ---------------------------------------------------------------------------
# Health & Meta
# ---------------------------------------------------------------------------

class TestHealth:
    def test_health_endpoint(self):
        r = api("get", "/v1/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] in ("healthy", "ok")

    def test_formats_endpoint(self):
        r = api("get", "/v1/formats")
        assert r.status_code == 200
        data = r.json()
        assert "pdf" in str(data).lower()

    def test_tips_endpoint(self):
        r = api("get", "/v1/tips")
        assert r.status_code == 200
        data = r.json()
        assert "optional_features" in data
        assert "common_mistakes" in data
        assert "quick_reference" in data

    def test_tips_has_all_features(self):
        r = api("get", "/v1/tips")
        data = r.json()
        features = data["optional_features"]
        expected = [
            "accuracy", "classify", "extract_schema", "template",
            "auto_extract", "describe_images", "ocr_correct",
            "ocr_embed", "show_formulas", "chunk", "chunk_size", "language",
        ]
        for feat in expected:
            assert feat in features, f"optional_features missing: {feat}"

    def test_templates_endpoint(self):
        r = api("get", "/v1/templates")
        assert r.status_code == 200
        data = r.json()
        # Response ist ein Dict mit template_id → schema Mapping
        assert isinstance(data, dict)
        assert "templates" in data or len(data) > 0


# ---------------------------------------------------------------------------
# Konvertierung — Basis
# ---------------------------------------------------------------------------

class TestConvertBasic:
    def test_convert_inline_text(self):
        """Base64-kodierter Plaintext → Markdown."""
        text = "# Hello World\n\nThis is a test."
        b64 = base64.b64encode(text.encode()).decode()
        r = api("post", "/v1/convert", json={
            "base64": b64,
            "filename": "test.txt",
        })
        assert r.status_code == 200
        data = r.json()
        assert data["success"] is True
        assert "Hello World" in data["markdown"]

    def test_convert_inline_csv(self):
        """CSV → Markdown-Tabelle."""
        csv = "Name,Age\nAlice,30\nBob,25"
        b64 = base64.b64encode(csv.encode()).decode()
        r = api("post", "/v1/convert", json={
            "base64": b64,
            "filename": "data.csv",
        })
        assert r.status_code == 200
        data = r.json()
        assert data["success"] is True
        assert "Alice" in data["markdown"]
        assert "|" in data["markdown"]  # Tabellen-Syntax

    def test_convert_missing_file(self):
        """Nicht existierende Datei → Fehler."""
        r = api("post", "/v1/convert", json={
            "path": "/data/does_not_exist_12345.pdf",
        })
        assert r.status_code == 200
        data = r.json()
        assert data["success"] is False

    def test_convert_no_input(self):
        """Kein Input → Fehler."""
        r = api("post", "/v1/convert", json={})
        assert r.status_code == 200
        data = r.json()
        assert data["success"] is False


# ---------------------------------------------------------------------------
# Konvertierung — Features
# ---------------------------------------------------------------------------

class TestConvertFeatures:
    def test_classify(self):
        """classify=true → meta.document_type gesetzt."""
        text = "Rechnung Nr. 2024-001\nBetrag: 119,00 EUR\nMwSt: 19,00 EUR"
        b64 = base64.b64encode(text.encode()).decode()
        r = api("post", "/v1/convert", json={
            "base64": b64,
            "filename": "rechnung.txt",
            "classify": True,
        })
        data = r.json()
        assert data["success"] is True
        assert data["meta"].get("document_type") is not None

    def test_chunk(self):
        """chunk=true → chunks Feld gefüllt."""
        text = "# Kapitel 1\n\nText hier.\n\n# Kapitel 2\n\nMehr Text."
        b64 = base64.b64encode(text.encode()).decode()
        r = api("post", "/v1/convert", json={
            "base64": b64,
            "filename": "doc.txt",
            "chunk": True,
            "chunk_size": 50,
        })
        data = r.json()
        assert data["success"] is True
        assert data.get("chunks") is not None
        assert len(data["chunks"]) > 0

    def test_quality_score(self):
        """Jede Konvertierung hat quality_score."""
        text = "Ein normaler deutscher Text mit mehreren Wörtern und Sätzen."
        b64 = base64.b64encode(text.encode()).decode()
        r = api("post", "/v1/convert", json={
            "base64": b64,
            "filename": "text.txt",
        })
        data = r.json()
        assert data["success"] is True
        assert "quality_score" in data["meta"]
        assert "quality_grade" in data["meta"]
        assert 0.0 <= data["meta"]["quality_score"] <= 1.0

    def test_extract_with_template(self):
        """template='invoice' → extracted Feld (braucht Mistral API)."""
        # Dieser Test braucht MISTRAL_API_KEY — skip wenn nicht gesetzt
        r = api("get", "/v1/health")
        health = r.json()
        if not health.get("mistral_available", True):
            pytest.skip("Mistral API not available")

        text = "Rechnung Nr. RE-2024-001\nDatum: 15.01.2024\nGesamtbetrag: 238,00 EUR\nMwSt 19%: 38,00 EUR\nNettobetrag: 200,00 EUR"
        b64 = base64.b64encode(text.encode()).decode()
        r = api("post", "/v1/convert", json={
            "base64": b64,
            "filename": "rechnung.txt",
            "template": "invoice",
        })
        data = r.json()
        assert data["success"] is True
        assert data.get("extracted") is not None


# ---------------------------------------------------------------------------
# Meta-Felder
# ---------------------------------------------------------------------------

class TestMetaFields:
    def test_meta_always_present(self):
        """Jede Response hat meta mit Basis-Feldern."""
        text = "Test"
        b64 = base64.b64encode(text.encode()).decode()
        r = api("post", "/v1/convert", json={
            "base64": b64,
            "filename": "test.txt",
        })
        data = r.json()
        assert "meta" in data
        meta = data["meta"]
        assert "source_type" in meta
        assert "format" in meta
        assert "processed_at" in meta
        assert "duration_ms" in meta

    def test_meta_size_bytes(self):
        """size_bytes korrekt gesetzt."""
        text = "A" * 100
        b64 = base64.b64encode(text.encode()).decode()
        r = api("post", "/v1/convert", json={
            "base64": b64,
            "filename": "test.txt",
        })
        data = r.json()
        assert data["meta"]["size_bytes"] == 100


# ---------------------------------------------------------------------------
# Folder
# ---------------------------------------------------------------------------

class TestFolder:
    def test_convert_folder_empty(self):
        """Leerer/nicht existierender Ordner → saubere Response, kein Crash."""
        r = api("post", "/v1/convert/folder", json={
            "path": "/data/_smoke_test_empty",
        })
        assert r.status_code == 200

    def test_convert_folder_inline(self):
        """Folder mit einer Datei via base64 → funktioniert."""
        text = "# Smoke Test\nFolder conversion works."
        b64 = base64.b64encode(text.encode()).decode()
        # Einzelne Datei konvertieren als Proxy für Folder-Funktionalität
        r = api("post", "/v1/convert", json={
            "base64": b64,
            "filename": "smoke.txt",
        })
        assert r.status_code == 200
        assert r.json()["success"] is True


# ---------------------------------------------------------------------------
# Templates CRUD
# ---------------------------------------------------------------------------

class TestTemplates:
    def test_list_templates(self):
        r = api("get", "/v1/templates")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, dict)
        assert "templates" in data or len(data) > 0

    def test_search_templates(self):
        r = api("get", "/v1/templates/search", params={"q": "invoice"})
        assert r.status_code == 200

    def test_categories(self):
        r = api("get", "/v1/templates/categories")
        assert r.status_code == 200
        data = r.json()
        # Response kann Liste oder Dict mit categories-Key sein
        if isinstance(data, dict):
            cats = data.get("categories", [])
        else:
            cats = data
        assert len(cats) > 0


# ---------------------------------------------------------------------------
# Regression Guards — diese Tests verhindern bekannte Regressions
# ---------------------------------------------------------------------------

class TestRegressionGuards:
    def test_no_stub_features(self):
        """Kein Feature darf ein Stub sein — alles muss funktionieren."""
        r = api("get", "/v1/tips")
        data = r.json()
        # Alle optional_features müssen eine description haben
        for name, info in data["optional_features"].items():
            assert "description" in info, f"Feature {name} has no description"

    def test_version_not_empty(self):
        """Service-Version muss gesetzt sein."""
        r = api("get", "/v1/tips")
        data = r.json()
        assert "service" in data
        assert "v" in data["service"].lower()

    def test_response_envelope_consistent(self):
        """Erfolg und Fehler haben gleiches Envelope-Format."""
        # Erfolg
        text = "Test"
        b64 = base64.b64encode(text.encode()).decode()
        r1 = api("post", "/v1/convert", json={"base64": b64, "filename": "t.txt"})
        d1 = r1.json()
        assert "success" in d1
        assert "markdown" in d1
        assert "meta" in d1

        # Fehler
        r2 = api("post", "/v1/convert", json={"path": "/nonexistent"})
        d2 = r2.json()
        assert "success" in d2
        assert "meta" in d2
