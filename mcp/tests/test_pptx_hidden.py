"""
Unit-Tests für T-MKIT-030: PPTX Hidden Slides + Embedded Excel aus Charts.

Tests prüfen:
- Keine versteckten Slides → hidden_slide_count=0
- Versteckte Slides werden erkannt
- Korrekte Slide-Nummern werden zurückgegeben
- Eingebettete Excel-Objekte werden aufgelistet
- Nicht-PPTX-Dateien → None
- meta.hidden_slides wird gesetzt
- Hinweis im Markdown wird eingefügt
"""

import io
import zipfile
from unittest.mock import patch

import pytest

from conftest import load_server_module, run_async


# =============================================================================
# Modul-Setup
# =============================================================================

_server = load_server_module(use_real_pil=False)

extract_pptx_hidden_info = _server.extract_pptx_hidden_info
convert_auto = _server.convert_auto


# =============================================================================
# Hilfsfunktion: Minimales PPTX-ZIP erzeugen
# =============================================================================

def _make_pptx_zip(slides, embeddings=None):
    """
    Erstellt ein minimales PPTX-ZIP im Speicher.

    Args:
        slides: Liste von dicts mit 'number' und 'hidden' (bool)
        embeddings: Optionale Liste von Dateinamen in ppt/embeddings/

    Returns:
        bytes — gültiges ZIP mit den angegebenen Slide-XMLs
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for s in slides:
            show_attr = ' show="0"' if s.get("hidden") else ""
            xml = (
                '<?xml version="1.0"?>'
                '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
                f'{show_attr}>'
                "<p:cSld><p:spTree/></p:cSld>"
                "</p:sld>"
            )
            zf.writestr(f"ppt/slides/slide{s['number']}.xml", xml)

        if embeddings:
            for emb_name in embeddings:
                zf.writestr(f"ppt/embeddings/{emb_name}", b"fake-content")

    return buf.getvalue()


# =============================================================================
# Tests
# =============================================================================

class TestNoHiddenSlides:
    """test_no_hidden_slides: Alle Slides sichtbar → hidden_slide_count=0."""

    def test_all_visible_slides_count_zero(self):
        """Keine versteckten Slides → hidden_slide_count ist 0."""
        pptx_data = _make_pptx_zip([
            {"number": 1, "hidden": False},
            {"number": 2, "hidden": False},
            {"number": 3, "hidden": False},
        ])
        result = extract_pptx_hidden_info(pptx_data)
        assert result is not None
        assert result["hidden_slide_count"] == 0
        assert result["hidden_slide_numbers"] == []

    def test_all_visible_no_embedded_objects(self):
        """Keine eingebetteten Objekte → leere Liste."""
        pptx_data = _make_pptx_zip([{"number": 1, "hidden": False}])
        result = extract_pptx_hidden_info(pptx_data)
        assert result is not None
        assert result["embedded_objects"] == []


class TestHiddenSlidesDetected:
    """test_hidden_slides_detected: Versteckte Slides werden erkannt."""

    def test_single_hidden_slide_detected(self):
        """Ein versteckter Slide wird korrekt erkannt."""
        pptx_data = _make_pptx_zip([
            {"number": 1, "hidden": False},
            {"number": 2, "hidden": True},
        ])
        result = extract_pptx_hidden_info(pptx_data)
        assert result is not None
        assert result["hidden_slide_count"] == 1

    def test_multiple_hidden_slides_detected(self):
        """Mehrere versteckte Slides werden alle erkannt."""
        pptx_data = _make_pptx_zip([
            {"number": 1, "hidden": False},
            {"number": 2, "hidden": True},
            {"number": 3, "hidden": False},
            {"number": 4, "hidden": True},
            {"number": 5, "hidden": True},
        ])
        result = extract_pptx_hidden_info(pptx_data)
        assert result is not None
        assert result["hidden_slide_count"] == 3


class TestHiddenSlideNumbers:
    """test_hidden_slide_numbers: Korrekte Slide-Nummern werden zurückgegeben."""

    def test_correct_slide_numbers_returned(self):
        """Die Slide-Nummern der versteckten Slides sind korrekt."""
        pptx_data = _make_pptx_zip([
            {"number": 1, "hidden": False},
            {"number": 2, "hidden": True},
            {"number": 3, "hidden": False},
            {"number": 4, "hidden": False},
            {"number": 5, "hidden": True},
        ])
        result = extract_pptx_hidden_info(pptx_data)
        assert result is not None
        assert result["hidden_slide_numbers"] == [2, 5]

    def test_slide_numbers_are_sorted(self):
        """Slide-Nummern werden sortiert zurückgegeben."""
        # Absichtlich nicht in Reihenfolge
        pptx_data = _make_pptx_zip([
            {"number": 5, "hidden": True},
            {"number": 1, "hidden": True},
            {"number": 3, "hidden": False},
        ])
        result = extract_pptx_hidden_info(pptx_data)
        assert result is not None
        assert result["hidden_slide_numbers"] == sorted(result["hidden_slide_numbers"])


class TestEmbeddedObjectsListed:
    """test_embedded_objects_listed: Eingebettete Excel-Objekte werden erkannt."""

    def test_xlsx_embedding_detected(self):
        """Eine eingebettete .xlsx-Datei wird in embedded_objects aufgelistet."""
        pptx_data = _make_pptx_zip(
            [{"number": 1, "hidden": False}],
            embeddings=["Microsoft_Excel_Worksheet1.xlsx"],
        )
        result = extract_pptx_hidden_info(pptx_data)
        assert result is not None
        assert "Microsoft_Excel_Worksheet1.xlsx" in result["embedded_objects"]

    def test_ole_object_bin_detected(self):
        """Eine eingebettete oleObject*.bin-Datei wird erkannt."""
        pptx_data = _make_pptx_zip(
            [{"number": 1, "hidden": False}],
            embeddings=["oleObject1.bin"],
        )
        result = extract_pptx_hidden_info(pptx_data)
        assert result is not None
        assert "oleObject1.bin" in result["embedded_objects"]

    def test_multiple_embeddings_detected(self):
        """Mehrere eingebettete Objekte werden alle aufgelistet."""
        pptx_data = _make_pptx_zip(
            [{"number": 1, "hidden": False}],
            embeddings=["oleObject1.bin", "chart1.xlsx", "oleObject2.bin"],
        )
        result = extract_pptx_hidden_info(pptx_data)
        assert result is not None
        assert len(result["embedded_objects"]) == 3

    def test_non_excel_embeddings_excluded(self):
        """Nicht-Excel-Einbettungen (z.B. .wmf) werden ignoriert."""
        pptx_data = _make_pptx_zip(
            [{"number": 1, "hidden": False}],
            embeddings=["image1.wmf", "oleObject1.bin"],
        )
        result = extract_pptx_hidden_info(pptx_data)
        assert result is not None
        # Nur oleObject1.bin, nicht image1.wmf
        assert "oleObject1.bin" in result["embedded_objects"]
        assert "image1.wmf" not in result["embedded_objects"]


class TestNoPptxReturnsNone:
    """test_no_pptx_returns_none: Nicht-PPTX-Daten → None."""

    def test_invalid_bytes_returns_none(self):
        """Ungültige Bytes (kein ZIP) → None."""
        result = extract_pptx_hidden_info(b"this is not a pptx file")
        assert result is None

    def test_pdf_like_bytes_returns_none(self):
        """PDF-ähnliche Bytes → None (kein gültiges ZIP)."""
        result = extract_pptx_hidden_info(b"%PDF-1.4 fake pdf content")
        assert result is None

    def test_empty_bytes_returns_none(self):
        """Leere Bytes → None."""
        result = extract_pptx_hidden_info(b"")
        assert result is None


class TestHiddenSlidesInMeta:
    """test_hidden_slides_in_meta: meta.hidden_slides wird gesetzt."""

    def test_hidden_slides_set_in_meta_via_convert_auto(self):
        """convert_auto setzt meta.hidden_slides wenn versteckte Slides vorhanden sind."""
        pptx_data = _make_pptx_zip([
            {"number": 1, "hidden": False},
            {"number": 2, "hidden": True},
            {"number": 3, "hidden": True},
        ])

        with patch.object(_server, "detect_mimetype_from_bytes", return_value=None), \
             patch.object(_server, "md") as mock_md:
            mock_result = mock_md.convert.return_value
            mock_result.text_content = "# Slide 1\n\nContent here."
            mock_result.title = None
            response = run_async(convert_auto(
                file_data=pptx_data,
                filename="presentation.pptx",
                source="test",
                source_type="base64",
                input_meta={},
                show_formulas=False,
            ))

        assert response.success is True
        assert response.meta.hidden_slides == 2

    def test_no_hidden_slides_meta_is_none(self):
        """Keine versteckten Slides → meta.hidden_slides ist None."""
        pptx_data = _make_pptx_zip([
            {"number": 1, "hidden": False},
            {"number": 2, "hidden": False},
        ])

        with patch.object(_server, "detect_mimetype_from_bytes", return_value=None), \
             patch.object(_server, "md") as mock_md:
            mock_result = mock_md.convert.return_value
            mock_result.text_content = "# Slide 1\n\nContent."
            mock_result.title = None
            response = run_async(convert_auto(
                file_data=pptx_data,
                filename="visible_only.pptx",
                source="test",
                source_type="base64",
                input_meta={},
                show_formulas=False,
            ))

        assert response.success is True
        assert response.meta.hidden_slides is None


class TestNoteInMarkdown:
    """test_note_in_markdown: Hinweis im Markdown wenn versteckte Slides vorhanden."""

    def test_hidden_slides_note_appended_to_markdown(self):
        """Wenn versteckte Slides vorhanden, wird ein Hinweis ans Markdown angehängt."""
        pptx_data = _make_pptx_zip([
            {"number": 1, "hidden": False},
            {"number": 2, "hidden": True},
            {"number": 5, "hidden": True},
        ])

        with patch.object(_server, "detect_mimetype_from_bytes", return_value=None), \
             patch.object(_server, "md") as mock_md:
            mock_result = mock_md.convert.return_value
            mock_result.text_content = "# My Presentation"
            mock_result.title = None
            response = run_async(convert_auto(
                file_data=pptx_data,
                filename="with_hidden.pptx",
                source="test",
                source_type="base64",
                input_meta={},
                show_formulas=False,
            ))

        assert response.success is True
        assert response.markdown is not None
        assert "hidden slide" in response.markdown.lower()
        assert "2" in response.markdown  # slide count
        assert "2, 5" in response.markdown  # slide numbers

    def test_no_note_when_no_hidden_slides(self):
        """Keine versteckten Slides → kein Hinweis im Markdown."""
        pptx_data = _make_pptx_zip([
            {"number": 1, "hidden": False},
            {"number": 2, "hidden": False},
        ])

        with patch.object(_server, "detect_mimetype_from_bytes", return_value=None), \
             patch.object(_server, "md") as mock_md:
            mock_result = mock_md.convert.return_value
            mock_result.text_content = "# Slide content"
            mock_result.title = None
            response = run_async(convert_auto(
                file_data=pptx_data,
                filename="no_hidden.pptx",
                source="test",
                source_type="base64",
                input_meta={},
                show_formulas=False,
            ))

        assert response.success is True
        assert response.markdown is not None
        assert "hidden slide" not in response.markdown.lower()
