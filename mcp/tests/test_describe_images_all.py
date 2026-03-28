"""
Tests für T-DAI-010: describe_images für alle Formate (PDF, ODT, ODP, HTML).

Erweitert den bestehenden DOCX/PPTX-Support um PDF, ODT, ODP und HTML.
Alle Tests laufen ohne Docker-Container und ohne echte API-Calls.
Externe Abhängigkeiten (fitz, PIL) werden echt genutzt wo verfügbar,
da die Extraktion echte Bibliotheken benötigt.
"""

import base64
import io
import zipfile
from pathlib import Path

import pytest

from conftest import (
    load_server_module,
    PNG_100x100 as _PNG_100x100,
    PNG_200x200 as _PNG_200x200,
    PNG_30x30 as _PNG_30x30,
    PNG_50x50 as _PNG_50x50,
)

# Modul mit echtem PIL laden (für Größen-Filterung)
_server = load_server_module(use_real_pil=True)


# ---------------------------------------------------------------------------
# Hilfsfunktionen zum Erzeugen von Test-Dateien
# ---------------------------------------------------------------------------

def _make_odt_zip(images: dict[str, bytes]) -> bytes:
    """Erstellt ein synthetisches ODT-ZIP mit Bildern in Pictures/."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in images.items():
            zf.writestr(f"Pictures/{name}", data)
    return buf.getvalue()


def _make_odp_zip(images: dict[str, bytes]) -> bytes:
    """Erstellt ein synthetisches ODP-ZIP mit Bildern in Pictures/."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in images.items():
            zf.writestr(f"Pictures/{name}", data)
    return buf.getvalue()


def _make_html_with_base64_img(img_bytes: bytes, img_type: str = "png") -> str:
    """Erstellt HTML-Content mit einem eingebetteten Base64-Bild."""
    b64 = base64.b64encode(img_bytes).decode("ascii")
    return f'<html><body><img src="data:image/{img_type};base64,{b64}" /></body></html>'


def _make_pdf_with_image(img_bytes: bytes) -> bytes:
    """
    Erstellt ein Mini-PDF mit einem eingebetteten Bild via PyMuPDF (fitz).

    Returns:
        PDF-Bytes mit einem eingebetteten Rasterbild.
    """
    import fitz  # noqa: PLC0415
    doc = fitz.open()
    page = doc.new_page(width=400, height=300)
    # Bild als PNG in das PDF einbetten
    img_rect = fitz.Rect(50, 50, 200, 150)
    page.insert_image(img_rect, stream=img_bytes)
    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


# ---------------------------------------------------------------------------
# Tests: extract_images_from_pdf
# ---------------------------------------------------------------------------

class TestExtractImagesFromPdf:
    """Tests für extract_images_from_pdf."""

    def test_extract_images_from_pdf_returns_images(self, tmp_path: Path) -> None:
        """Eingebettetes Bild aus PDF wird erkannt und zurückgegeben."""
        pdf_bytes = _make_pdf_with_image(_PNG_100x100)
        pdf_path = tmp_path / "test.pdf"
        pdf_path.write_bytes(pdf_bytes)

        images = _server.extract_images_from_pdf(pdf_path)

        assert isinstance(images, list)
        assert len(images) >= 1
        img = images[0]
        assert "name" in img
        assert "data" in img
        assert isinstance(img["data"], bytes)
        assert len(img["data"]) > 0
        assert "page_number" in img
        assert img["page_number"] == 1

    def test_extract_images_from_pdf_name_format(self, tmp_path: Path) -> None:
        """Extrahierte Bilder haben Namen im Format page<N>_img<M>.png."""
        pdf_bytes = _make_pdf_with_image(_PNG_100x100)
        pdf_path = tmp_path / "test.pdf"
        pdf_path.write_bytes(pdf_bytes)

        images = _server.extract_images_from_pdf(pdf_path)

        assert len(images) >= 1
        assert images[0]["name"].startswith("page1_img")
        assert images[0]["name"].endswith(".png")

    def test_extract_images_from_pdf_returns_empty_on_error(self, tmp_path: Path) -> None:
        """Bei ungültigem PDF wird eine leere Liste zurückgegeben (kein Crash)."""
        bad_path = tmp_path / "broken.pdf"
        bad_path.write_bytes(b"not a pdf file")

        images = _server.extract_images_from_pdf(bad_path)

        assert isinstance(images, list)
        assert len(images) == 0

    def test_extract_images_from_pdf_returns_list_dict(self, tmp_path: Path) -> None:
        """Rückgabe ist immer list[dict] mit name und data."""
        pdf_bytes = _make_pdf_with_image(_PNG_200x200)
        pdf_path = tmp_path / "test.pdf"
        pdf_path.write_bytes(pdf_bytes)

        images = _server.extract_images_from_pdf(pdf_path)

        assert isinstance(images, list)
        for img in images:
            assert isinstance(img, dict)
            assert "name" in img
            assert "data" in img


# ---------------------------------------------------------------------------
# Tests: extract_images_from_odt
# ---------------------------------------------------------------------------

class TestExtractImagesFromOdt:
    """Tests für extract_images_from_odt."""

    def test_extract_images_from_odt_returns_images(self, tmp_path: Path) -> None:
        """Bilder aus Pictures/ werden erkannt und zurückgegeben."""
        odt_bytes = _make_odt_zip({"image.png": _PNG_100x100})
        odt_path = tmp_path / "test.odt"
        odt_path.write_bytes(odt_bytes)

        images = _server.extract_images_from_odt(odt_path)

        assert len(images) == 1
        assert images[0]["name"] == "image.png"
        assert images[0]["data"] == _PNG_100x100
        assert "position_hint" in images[0]

    def test_extract_images_from_odt_multiple(self, tmp_path: Path) -> None:
        """Mehrere Bilder werden alle extrahiert."""
        odt_bytes = _make_odt_zip({
            "img1.png": _PNG_100x100,
            "img2.png": _PNG_200x200,
        })
        odt_path = tmp_path / "multi.odt"
        odt_path.write_bytes(odt_bytes)

        images = _server.extract_images_from_odt(odt_path)

        assert len(images) == 2
        names = {img["name"] for img in images}
        assert names == {"img1.png", "img2.png"}

    def test_extract_images_from_odt_filters_small(self, tmp_path: Path) -> None:
        """Zu kleine Bilder (< MIN_IMAGE_SIZE_PX) werden herausgefiltert."""
        odt_bytes = _make_odt_zip({
            "small.png": _PNG_30x30,
            "large.png": _PNG_100x100,
        })
        odt_path = tmp_path / "mixed.odt"
        odt_path.write_bytes(odt_bytes)

        images = _server.extract_images_from_odt(odt_path)

        names = [img["name"] for img in images]
        assert "large.png" in names
        assert "small.png" not in names

    def test_extract_images_from_odt_returns_empty_on_error(self, tmp_path: Path) -> None:
        """Bei ungültigem ZIP wird eine leere Liste zurückgegeben."""
        bad_path = tmp_path / "broken.odt"
        bad_path.write_bytes(b"not a zip")

        images = _server.extract_images_from_odt(bad_path)

        assert isinstance(images, list)
        assert len(images) == 0

    def test_extract_images_from_odt_boundary_size(self, tmp_path: Path) -> None:
        """Bilder exakt bei MIN_IMAGE_SIZE_PX (50x50) werden nicht übersprungen."""
        odt_bytes = _make_odt_zip({"boundary.png": _PNG_50x50})
        odt_path = tmp_path / "boundary.odt"
        odt_path.write_bytes(odt_bytes)

        images = _server.extract_images_from_odt(odt_path)

        assert len(images) == 1
        assert images[0]["name"] == "boundary.png"


# ---------------------------------------------------------------------------
# Tests: extract_images_from_odp
# ---------------------------------------------------------------------------

class TestExtractImagesFromOdp:
    """Tests für extract_images_from_odp."""

    def test_extract_images_from_odp_returns_images(self, tmp_path: Path) -> None:
        """Bilder aus Pictures/ werden erkannt und zurückgegeben."""
        odp_bytes = _make_odp_zip({"slide_image.png": _PNG_100x100})
        odp_path = tmp_path / "test.odp"
        odp_path.write_bytes(odp_bytes)

        images = _server.extract_images_from_odp(odp_path)

        assert len(images) == 1
        assert images[0]["name"] == "slide_image.png"
        assert images[0]["data"] == _PNG_100x100
        assert "position_hint" in images[0]

    def test_extract_images_from_odp_multiple(self, tmp_path: Path) -> None:
        """Mehrere Bilder werden alle extrahiert."""
        odp_bytes = _make_odp_zip({
            "img1.png": _PNG_100x100,
            "img2.png": _PNG_200x200,
        })
        odp_path = tmp_path / "multi.odp"
        odp_path.write_bytes(odp_bytes)

        images = _server.extract_images_from_odp(odp_path)

        assert len(images) == 2

    def test_extract_images_from_odp_filters_small(self, tmp_path: Path) -> None:
        """Zu kleine Bilder werden herausgefiltert."""
        odp_bytes = _make_odp_zip({
            "small.png": _PNG_30x30,
            "large.png": _PNG_100x100,
        })
        odp_path = tmp_path / "mixed.odp"
        odp_path.write_bytes(odp_bytes)

        images = _server.extract_images_from_odp(odp_path)

        names = [img["name"] for img in images]
        assert "large.png" in names
        assert "small.png" not in names

    def test_extract_images_from_odp_returns_empty_on_error(self, tmp_path: Path) -> None:
        """Bei ungültigem ZIP wird eine leere Liste zurückgegeben."""
        bad_path = tmp_path / "broken.odp"
        bad_path.write_bytes(b"not a zip")

        images = _server.extract_images_from_odp(bad_path)

        assert isinstance(images, list)
        assert len(images) == 0


# ---------------------------------------------------------------------------
# Tests: extract_images_from_html
# ---------------------------------------------------------------------------

class TestExtractImagesFromHtml:
    """Tests für extract_images_from_html."""

    def test_extract_images_from_html_base64_img(self, tmp_path: Path) -> None:
        """Base64-eingebettetes Bild wird erkannt und dekodiert."""
        html_content = _make_html_with_base64_img(_PNG_100x100, "png")
        html_path = tmp_path / "test.html"
        html_path.write_text(html_content, encoding="utf-8")

        images = _server.extract_images_from_html(html_path)

        assert len(images) == 1
        assert images[0]["name"] == "html_img0.png"
        assert isinstance(images[0]["data"], bytes)
        assert len(images[0]["data"]) > 0
        assert "position_hint" in images[0]

    def test_extract_images_from_html_multiple_imgs(self, tmp_path: Path) -> None:
        """Mehrere Base64-Bilder werden alle extrahiert."""
        b64_1 = base64.b64encode(_PNG_100x100).decode("ascii")
        b64_2 = base64.b64encode(_PNG_200x200).decode("ascii")
        html_content = (
            f'<html><body>'
            f'<img src="data:image/png;base64,{b64_1}" />'
            f'<img src="data:image/png;base64,{b64_2}" />'
            f'</body></html>'
        )
        html_path = tmp_path / "multi.html"
        html_path.write_text(html_content, encoding="utf-8")

        images = _server.extract_images_from_html(html_path)

        assert len(images) == 2
        names = {img["name"] for img in images}
        assert "html_img0.png" in names
        assert "html_img1.png" in names

    def test_extract_images_from_html_filters_small(self, tmp_path: Path) -> None:
        """Zu kleine Base64-Bilder werden herausgefiltert."""
        b64_small = base64.b64encode(_PNG_30x30).decode("ascii")
        b64_large = base64.b64encode(_PNG_100x100).decode("ascii")
        html_content = (
            f'<html><body>'
            f'<img src="data:image/png;base64,{b64_small}" />'
            f'<img src="data:image/png;base64,{b64_large}" />'
            f'</body></html>'
        )
        html_path = tmp_path / "mixed.html"
        html_path.write_text(html_content, encoding="utf-8")

        images = _server.extract_images_from_html(html_path)

        # Nur das große Bild wird extrahiert
        assert len(images) == 1
        # Das große Bild hat Index 1, weil html_img0 (klein) übersprungen wurde
        # Aber der Index kommt von re.findall Reihenfolge — img0 ist klein, img1 ist groß
        assert images[0]["name"] == "html_img1.png"

    def test_extract_images_from_html_no_images(self, tmp_path: Path) -> None:
        """HTML ohne Base64-Bilder gibt leere Liste zurück."""
        html_content = "<html><body><p>Kein Bild hier.</p></body></html>"
        html_path = tmp_path / "empty.html"
        html_path.write_text(html_content, encoding="utf-8")

        images = _server.extract_images_from_html(html_path)

        assert isinstance(images, list)
        assert len(images) == 0

    def test_extract_images_from_html_external_src_ignored(self, tmp_path: Path) -> None:
        """Externe URLs (nicht base64) werden ignoriert."""
        html_content = '<html><body><img src="https://example.com/img.png" /></body></html>'
        html_path = tmp_path / "external.html"
        html_path.write_text(html_content, encoding="utf-8")

        images = _server.extract_images_from_html(html_path)

        assert isinstance(images, list)
        assert len(images) == 0

    def test_extract_images_from_html_returns_empty_on_missing_file(self, tmp_path: Path) -> None:
        """Bei nicht existierender Datei wird eine leere Liste zurückgegeben."""
        missing_path = tmp_path / "missing.html"

        images = _server.extract_images_from_html(missing_path)

        assert isinstance(images, list)
        assert len(images) == 0


# ---------------------------------------------------------------------------
# Tests: Bestehende DOCX/PPTX-Extraktion funktioniert weiterhin
# ---------------------------------------------------------------------------

class TestExistingDocxPptxStillWorks:
    """Regression-Tests: DOCX/PPTX-Extraktion ist nicht beschädigt."""

    def test_docx_extraction_still_works(self, tmp_path: Path) -> None:
        """extract_images_from_docx gibt weiterhin list[dict] mit name+data zurück."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("word/media/image1.png", _PNG_100x100)
        docx_path = tmp_path / "test.docx"
        docx_path.write_bytes(buf.getvalue())

        images = _server.extract_images_from_docx(docx_path)

        assert len(images) == 1
        assert images[0]["name"] == "image1.png"
        assert images[0]["data"] == _PNG_100x100
        assert "position_hint" in images[0]

    def test_pptx_extraction_still_works(self, tmp_path: Path) -> None:
        """extract_images_from_pptx gibt weiterhin list[dict] mit name+data zurück."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("ppt/media/slide_img.png", _PNG_200x200)
        pptx_path = tmp_path / "test.pptx"
        pptx_path.write_bytes(buf.getvalue())

        images = _server.extract_images_from_pptx(pptx_path)

        assert len(images) == 1
        assert images[0]["name"] == "slide_img.png"
        assert images[0]["data"] == _PNG_200x200
        assert "slide_number" in images[0]
