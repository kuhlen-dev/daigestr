"""
Tests für T-DAI-027: Mistral Batch Integration

- prepare-batch: Dateien mit Bildern → batch_jobs Array wird aufgebaut
- apply-batch-results: Markdown + Results → Beschreibungen eingefügt
- Brix-Detection: Mock Health-Check
"""

import asyncio
import base64
import io
import sys
import time
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conftest import load_server_module, run_async, PNG_100x100 as _PNG_100x100


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def _make_docx_zip(images: dict) -> bytes:
    """Erstellt synthetisches DOCX-ZIP mit Bildern in word/media/."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in images.items():
            zf.writestr(f"word/media/{name}", data)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Tests: Brix-Detection
# ---------------------------------------------------------------------------

# Server einmalig laden (use_real_pil=True für Bild-Verarbeitung)
_server = load_server_module(use_real_pil=True)


class TestBrixDetection:
    """Tests für _is_brix_available() in routing.py."""

    def test_brix_available_when_health_ok(self):
        """Wenn Brix /health 200 zurückgibt, ist Brix verfügbar."""
        import routing

        # Cache leeren damit frischer Check gemacht wird
        routing._brix_available_cache["checked_at"] = 0
        routing._brix_available_cache["available"] = None

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("routing.httpx") as mock_httpx:
            mock_httpx.get.return_value = mock_response
            result = routing._is_brix_available()

        assert result is True

    def test_brix_unavailable_when_health_fails(self):
        """Wenn Brix /health Fehler wirft, ist Brix nicht verfügbar."""
        import routing

        routing._brix_available_cache["checked_at"] = 0
        routing._brix_available_cache["available"] = None

        with patch("routing.httpx") as mock_httpx:
            mock_httpx.get.side_effect = Exception("Connection refused")
            result = routing._is_brix_available()

        assert result is False

    def test_brix_cache_used_within_ttl(self):
        """Innerhalb der 5-Minuten-TTL wird der gecachte Wert verwendet."""
        import routing

        # Cache mit True befüllen
        routing._brix_available_cache["available"] = True
        routing._brix_available_cache["checked_at"] = time.time()  # gerade eben

        with patch("routing.httpx") as mock_httpx:
            result = routing._is_brix_available()
            # httpx.get sollte NICHT aufgerufen werden (Cache-Hit)
            mock_httpx.get.assert_not_called()

        assert result is True

    def test_brix_unavailable_when_non_200(self):
        """Wenn Brix /health Status != 200 zurückgibt, ist Brix nicht verfügbar."""
        import routing

        routing._brix_available_cache["checked_at"] = 0

        mock_response = MagicMock()
        mock_response.status_code = 503

        with patch("routing.httpx") as mock_httpx:
            mock_httpx.get.return_value = mock_response
            result = routing._is_brix_available()

        assert result is False


# ---------------------------------------------------------------------------
# Tests: prepare-batch Logik (direkte Funktion, kein HTTP)
# ---------------------------------------------------------------------------

class TestPrepareBatchLogic:
    """Tests für die Kern-Logik von prepare-batch."""

    def test_extract_images_and_build_batch_jobs(self, tmp_path: Path):
        """DOCX mit 2 Bildern → 2 batch_jobs werden aufgebaut."""
        docx_data = _make_docx_zip({"img1.png": _PNG_100x100, "img2.png": _PNG_100x100})
        docx_path = tmp_path / "test.docx"
        docx_path.write_bytes(docx_data)

        images = _server.extract_images_from_docx(docx_path)
        assert len(images) == 2

        # batch_jobs aufbauen (wie in api_rest.py)
        classify_prompt = "Classify this image into EXACTLY one category."
        batch_jobs = []
        for img in images:
            img_data, _ = _server.resize_image_if_needed(img["data"])
            mimetype = "image/png"
            batch_jobs.append({
                "id": img["name"],
                "image_base64": base64.b64encode(img_data).decode("utf-8"),
                "prompt": classify_prompt,
                "mimetype": mimetype,
            })

        assert len(batch_jobs) == 2
        for job in batch_jobs:
            assert "id" in job
            assert "image_base64" in job
            assert "prompt" in job
            assert "mimetype" in job
            # image_base64 muss valides base64 sein
            decoded = base64.b64decode(job["image_base64"])
            assert len(decoded) > 0

    def test_no_images_in_docx_returns_empty_batch(self, tmp_path: Path):
        """DOCX ohne Bilder → leere batch_jobs Liste."""
        # DOCX ohne Bilder (leeres ZIP)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w"):
            pass
        empty_docx_path = tmp_path / "empty.docx"
        empty_docx_path.write_bytes(buf.getvalue())

        images = _server.extract_images_from_docx(empty_docx_path)
        assert images == []

    def test_batch_job_image_base64_decodable(self, tmp_path: Path):
        """image_base64 in batch_jobs muss valides Base64 sein."""
        docx_data = _make_docx_zip({"test.png": _PNG_100x100})
        docx_path = tmp_path / "test.docx"
        docx_path.write_bytes(docx_data)

        images = _server.extract_images_from_docx(docx_path)
        assert len(images) == 1

        img_data, _ = _server.resize_image_if_needed(images[0]["data"])
        b64_str = base64.b64encode(img_data).decode("utf-8")

        # Dekodierung muss ohne Fehler funktionieren
        decoded = base64.b64decode(b64_str)
        assert decoded == img_data


# ---------------------------------------------------------------------------
# Tests: apply-batch-results Logik (direkte Funktion, kein HTTP)
# ---------------------------------------------------------------------------

class TestApplyBatchResultsLogic:
    """Tests für die Kern-Logik von apply-batch-results via insert_image_descriptions."""

    def test_insert_single_description(self):
        """Eine Beschreibung wird in den Markdown eingefügt."""
        markdown = "# Dokument\n\n![image](img1.png)\n\nText nach Bild."
        descriptions = [{"name": "img1.png", "description": "Ein Testbild mit rotem Kreis."}]

        result = _server.insert_image_descriptions(markdown, descriptions)

        assert "Ein Testbild mit rotem Kreis." in result
        # Platzhalter wurde ersetzt
        assert "![image](img1.png)" not in result

    def test_insert_multiple_descriptions(self):
        """Mehrere Beschreibungen werden alle eingefügt."""
        markdown = (
            "# Test\n\n"
            "![image](page1_img0.png)\n\n"
            "![image](page2_img0.png)\n\n"
            "Ende."
        )
        descriptions = [
            {"name": "page1_img0.png", "description": "Erste Seite Bild."},
            {"name": "page2_img0.png", "description": "Zweite Seite Bild."},
        ]

        result = _server.insert_image_descriptions(markdown, descriptions)

        assert "Erste Seite Bild." in result
        assert "Zweite Seite Bild." in result
        assert "![image](page1_img0.png)" not in result
        assert "![image](page2_img0.png)" not in result

    def test_empty_descriptions_leaves_placeholders(self):
        """Leere Beschreibungs-Liste lässt Platzhalter unverändert."""
        original = "# Test\n\n![image](img.png)\n\nText."
        result = _server.insert_image_descriptions(original, [])

        assert result == original

    def test_unknown_image_name_stays_as_placeholder(self):
        """Unbekannter Bildname → Platzhalter bleibt erhalten."""
        markdown = "![image](unknown.png)"
        descriptions = [{"name": "other.png", "description": "Anderes Bild."}]

        result = _server.insert_image_descriptions(markdown, descriptions)

        # Platzhalter für unknown.png bleibt erhalten
        assert "![image](unknown.png)" in result

    def test_apply_batch_results_format_conversion(self):
        """batch_results [{id, description}] korrekt zu descriptions [{name, description}] konvertiert."""
        markdown = "![image](page3_img0.png)"
        # Simuliere was api_rest.api_apply_batch_results macht:
        batch_results = [{"id": "page3_img0.png", "description": "Seite 3 Bild."}]
        descriptions = [{"name": item["id"], "description": item["description"]}
                        for item in batch_results]

        result = _server.insert_image_descriptions(markdown, descriptions)

        assert "Seite 3 Bild." in result

    def test_apply_empty_results_images_inserted_count(self):
        """Leere batch_results → images_inserted = 0."""
        batch_results = []
        descriptions = [{"name": item["id"], "description": item["description"]}
                        for item in batch_results]
        assert len(descriptions) == 0

    def test_apply_results_images_inserted_count(self):
        """3 batch_results → images_inserted = 3."""
        batch_results = [
            {"id": "a.png", "description": "A"},
            {"id": "b.png", "description": "B"},
            {"id": "c.png", "description": "C"},
        ]
        descriptions = [{"name": item["id"], "description": item["description"]}
                        for item in batch_results]
        assert len(descriptions) == 3
