"""
Tests für T-MKIT-025: PDF XMP Metadata + Embedded Files Extraktion.

Alle Tests laufen ohne Docker-Container und ohne echte PDF-Dateien.
PyMuPDF (fitz) wird vollständig gemockt.

Acceptance Criteria:
- extract_xmp_metadata: Basis-Felder (title, author)
- extract_xmp_metadata: PDF/A Level aus XMP-Stream
- extract_xmp_metadata: Graceful wenn PyMuPDF nicht verfügbar
- list_embedded_files: Dateiliste korrekt
- list_embedded_files: ZUGFeRD-Dateien werden NICHT zurückgegeben
- meta.xmp_metadata wird bei PDF-Konvertierung gesetzt
- meta.embedded_files wird bei PDF-Konvertierung gesetzt
- Für Nicht-PDF-Dateien keine XMP-Extraktion
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from conftest import load_server_module, run_async


# ---------------------------------------------------------------------------
# Modul laden (einmal für alle Tests in dieser Datei)
# ---------------------------------------------------------------------------

_server = load_server_module(use_real_pil=False)


# ---------------------------------------------------------------------------
# Minimales XMP XML für Tests
# ---------------------------------------------------------------------------

SAMPLE_XMP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
    <rdf:Description rdf:about=""
        xmlns:pdfaid="http://www.aiim.org/pdfa/ns/id/"
        xmlns:xmpMM="http://ns.adobe.com/xap/1.0/mm/"
        xmlns:xmp="http://ns.adobe.com/xap/1.0/">
      <pdfaid:part>3</pdfaid:part>
      <pdfaid:conformance>B</pdfaid:conformance>
      <xmpMM:DocumentID>uuid:abc-123-def</xmpMM:DocumentID>
      <xmpMM:InstanceID>uuid:xyz-789</xmpMM:InstanceID>
      <xmp:CreatorTool>Adobe InDesign 17.0</xmp:CreatorTool>
    </rdf:Description>
  </rdf:RDF>
</x:xmpmeta>"""


def _make_mock_fitz_with_xmp(
    metadata: dict,
    xmp_xml: str = "",
    embfile_names: list | None = None,
    embfile_info_map: dict | None = None,
) -> MagicMock:
    """Erstellt einen vollständigen fitz-Mock mit konfigurierbaren Metadaten."""
    mock_doc = MagicMock()
    mock_doc.metadata = metadata
    mock_doc.get_xml_metadata = MagicMock(return_value=xmp_xml)
    mock_doc.embfile_names = MagicMock(return_value=embfile_names or [])

    def _embfile_info(name: str) -> dict:
        if embfile_info_map and name in embfile_info_map:
            return embfile_info_map[name]
        return {"size": 1024, "desc": ""}

    mock_doc.embfile_info = MagicMock(side_effect=_embfile_info)
    mock_doc.close = MagicMock()

    mock_fitz = MagicMock()
    mock_fitz.open = MagicMock(return_value=mock_doc)
    return mock_fitz


# ---------------------------------------------------------------------------
# Tests: extract_xmp_metadata — Basis-Felder
# ---------------------------------------------------------------------------

class TestExtractXmpBasicMetadata:
    """Tests für extract_xmp_metadata() — Basis-Felder aus doc.metadata."""

    def test_extract_xmp_basic_title(self) -> None:
        """title aus doc.metadata wird korrekt zurückgegeben."""
        mock_fitz = _make_mock_fitz_with_xmp(
            metadata={"title": "Mein Dokument", "author": "Max Mustermann"},
            xmp_xml="",
        )
        with patch.dict(sys.modules, {"fitz": mock_fitz}):
            _server.PYMUPDF_AVAILABLE = True
            _server.fitz = mock_fitz
            result = _server.extract_xmp_metadata(b"%PDF-1.4 fake")

        assert result is not None
        assert result["title"] == "Mein Dokument"

    def test_extract_xmp_basic_author(self) -> None:
        """author aus doc.metadata wird korrekt zurückgegeben."""
        mock_fitz = _make_mock_fitz_with_xmp(
            metadata={"title": "Test", "author": "Max Mustermann"},
            xmp_xml="",
        )
        with patch.dict(sys.modules, {"fitz": mock_fitz}):
            _server.PYMUPDF_AVAILABLE = True
            _server.fitz = mock_fitz
            result = _server.extract_xmp_metadata(b"%PDF-1.4 fake")

        assert result is not None
        assert result["author"] == "Max Mustermann"

    def test_extract_xmp_empty_metadata_returns_none(self) -> None:
        """Leere Metadaten und kein XMP → None."""
        mock_fitz = _make_mock_fitz_with_xmp(metadata={}, xmp_xml="")
        with patch.dict(sys.modules, {"fitz": mock_fitz}):
            _server.PYMUPDF_AVAILABLE = True
            _server.fitz = mock_fitz
            result = _server.extract_xmp_metadata(b"%PDF-1.4 fake")

        assert result is None

    def test_extract_xmp_returns_dict(self) -> None:
        """extract_xmp_metadata gibt immer ein Dict oder None zurück."""
        mock_fitz = _make_mock_fitz_with_xmp(
            metadata={"creator": "Adobe Acrobat"},
            xmp_xml="",
        )
        with patch.dict(sys.modules, {"fitz": mock_fitz}):
            _server.PYMUPDF_AVAILABLE = True
            _server.fitz = mock_fitz
            result = _server.extract_xmp_metadata(b"%PDF-1.4 fake")

        assert result is None or isinstance(result, dict)


# ---------------------------------------------------------------------------
# Tests: extract_xmp_metadata — XMP-Stream / PDF/A Level
# ---------------------------------------------------------------------------

class TestExtractXmpPdfALevel:
    """Tests für extract_xmp_metadata() — XMP-Stream-Parsing, PDF/A Level."""

    def test_extract_xmp_pdf_a_level(self) -> None:
        """PDF/A Level wird aus XMP-Stream extrahiert (pdfaid:part + conformance)."""
        mock_fitz = _make_mock_fitz_with_xmp(
            metadata={"title": "PDF/A Dokument"},
            xmp_xml=SAMPLE_XMP_XML,
        )
        with patch.dict(sys.modules, {"fitz": mock_fitz}):
            _server.PYMUPDF_AVAILABLE = True
            _server.fitz = mock_fitz
            result = _server.extract_xmp_metadata(b"%PDF-1.4 fake")

        assert result is not None
        assert result.get("pdf_a_level") == "3B"

    def test_extract_xmp_document_id(self) -> None:
        """xmpMM:DocumentID wird aus XMP-Stream extrahiert."""
        mock_fitz = _make_mock_fitz_with_xmp(
            metadata={},
            xmp_xml=SAMPLE_XMP_XML,
        )
        with patch.dict(sys.modules, {"fitz": mock_fitz}):
            _server.PYMUPDF_AVAILABLE = True
            _server.fitz = mock_fitz
            result = _server.extract_xmp_metadata(b"%PDF-1.4 fake")

        assert result is not None
        assert result.get("document_id") == "uuid:abc-123-def"

    def test_extract_xmp_instance_id(self) -> None:
        """xmpMM:InstanceID wird aus XMP-Stream extrahiert."""
        mock_fitz = _make_mock_fitz_with_xmp(
            metadata={},
            xmp_xml=SAMPLE_XMP_XML,
        )
        with patch.dict(sys.modules, {"fitz": mock_fitz}):
            _server.PYMUPDF_AVAILABLE = True
            _server.fitz = mock_fitz
            result = _server.extract_xmp_metadata(b"%PDF-1.4 fake")

        assert result is not None
        assert result.get("instance_id") == "uuid:xyz-789"

    def test_extract_xmp_creator_tool(self) -> None:
        """xmp:CreatorTool wird aus XMP-Stream extrahiert."""
        mock_fitz = _make_mock_fitz_with_xmp(
            metadata={},
            xmp_xml=SAMPLE_XMP_XML,
        )
        with patch.dict(sys.modules, {"fitz": mock_fitz}):
            _server.PYMUPDF_AVAILABLE = True
            _server.fitz = mock_fitz
            result = _server.extract_xmp_metadata(b"%PDF-1.4 fake")

        assert result is not None
        assert result.get("creator_tool") == "Adobe InDesign 17.0"

    def test_extract_xmp_invalid_xml_graceful(self) -> None:
        """Ungültiges XMP-XML führt nicht zu einem Fehler — Basis-Meta wird zurückgegeben."""
        mock_fitz = _make_mock_fitz_with_xmp(
            metadata={"title": "Robustes Dokument"},
            xmp_xml="<<<invalid xml>>>",
        )
        with patch.dict(sys.modules, {"fitz": mock_fitz}):
            _server.PYMUPDF_AVAILABLE = True
            _server.fitz = mock_fitz
            result = _server.extract_xmp_metadata(b"%PDF-1.4 fake")

        # Basis-Metadaten sollten trotzdem da sein
        assert result is not None
        assert result["title"] == "Robustes Dokument"
        # XMP-Felder fehlen, aber kein Crash
        assert "pdf_a_level" not in result


# ---------------------------------------------------------------------------
# Tests: extract_xmp_metadata — Graceful ohne PyMuPDF
# ---------------------------------------------------------------------------

class TestExtractXmpNoPymupdf:
    """Tests für extract_xmp_metadata() ohne PyMuPDF."""

    def test_extract_xmp_no_pymupdf(self) -> None:
        """Ohne PyMuPDF gibt extract_xmp_metadata None zurück."""
        original = _server.PYMUPDF_AVAILABLE
        try:
            _server.PYMUPDF_AVAILABLE = False
            result = _server.extract_xmp_metadata(b"%PDF-1.4 fake")
        finally:
            _server.PYMUPDF_AVAILABLE = original

        assert result is None

    def test_extract_xmp_fitz_error_graceful(self) -> None:
        """Wenn fitz.open() wirft, wird None zurückgegeben."""
        mock_fitz = MagicMock()
        mock_fitz.open = MagicMock(side_effect=Exception("corrupt PDF"))

        with patch.dict(sys.modules, {"fitz": mock_fitz}):
            _server.PYMUPDF_AVAILABLE = True
            _server.fitz = mock_fitz
            result = _server.extract_xmp_metadata(b"not a pdf")

        assert result is None


# ---------------------------------------------------------------------------
# Tests: list_embedded_files — Dateiliste
# ---------------------------------------------------------------------------

class TestListEmbeddedFiles:
    """Tests für list_embedded_files() Funktion."""

    def test_list_embedded_files_basic(self) -> None:
        """Eingebettete Dateien werden korrekt zurückgegeben."""
        mock_fitz = _make_mock_fitz_with_xmp(
            metadata={},
            embfile_names=["attachment.xlsx", "report.pdf"],
            embfile_info_map={
                "attachment.xlsx": {"size": 5000, "desc": "Anhang Tabelle"},
                "report.pdf": {"size": 12000, "desc": ""},
            },
        )
        with patch.dict(sys.modules, {"fitz": mock_fitz}):
            _server.PYMUPDF_AVAILABLE = True
            _server.fitz = mock_fitz
            result = _server.list_embedded_files(b"%PDF-1.4 fake")

        assert isinstance(result, list)
        assert len(result) == 2
        names = {entry["name"] for entry in result}
        assert "attachment.xlsx" in names
        assert "report.pdf" in names

    def test_list_embedded_files_size_present(self) -> None:
        """size-Feld wird aus embfile_info übernommen."""
        mock_fitz = _make_mock_fitz_with_xmp(
            metadata={},
            embfile_names=["data.csv"],
            embfile_info_map={"data.csv": {"size": 2048, "desc": "CSV Daten"}},
        )
        with patch.dict(sys.modules, {"fitz": mock_fitz}):
            _server.PYMUPDF_AVAILABLE = True
            _server.fitz = mock_fitz
            result = _server.list_embedded_files(b"%PDF-1.4 fake")

        assert len(result) == 1
        assert result[0]["name"] == "data.csv"
        assert result[0]["size"] == 2048

    def test_list_embedded_files_empty_pdf(self) -> None:
        """PDF ohne eingebettete Dateien → leere Liste."""
        mock_fitz = _make_mock_fitz_with_xmp(metadata={}, embfile_names=[])
        with patch.dict(sys.modules, {"fitz": mock_fitz}):
            _server.PYMUPDF_AVAILABLE = True
            _server.fitz = mock_fitz
            result = _server.list_embedded_files(b"%PDF-1.4 fake")

        assert result == []

    def test_list_embedded_files_returns_list(self) -> None:
        """list_embedded_files gibt immer eine Liste zurück."""
        mock_fitz = _make_mock_fitz_with_xmp(metadata={}, embfile_names=[])
        with patch.dict(sys.modules, {"fitz": mock_fitz}):
            _server.PYMUPDF_AVAILABLE = True
            _server.fitz = mock_fitz
            result = _server.list_embedded_files(b"%PDF-1.4 fake")

        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# Tests: list_embedded_files — ZUGFeRD-Dateien werden NICHT zurückgegeben
# ---------------------------------------------------------------------------

class TestListEmbeddedFilesExcludesZugferd:
    """ZUGFeRD-Dateien sollen von list_embedded_files ausgeschlossen werden."""

    def test_excludes_facturx_xml(self) -> None:
        """factur-x.xml (ZUGFeRD) wird NICHT in der Liste zurückgegeben."""
        mock_fitz = _make_mock_fitz_with_xmp(
            metadata={},
            embfile_names=["factur-x.xml", "attachment.xlsx"],
            embfile_info_map={
                "factur-x.xml": {"size": 3000, "desc": "ZUGFeRD Invoice"},
                "attachment.xlsx": {"size": 5000, "desc": ""},
            },
        )
        with patch.dict(sys.modules, {"fitz": mock_fitz}):
            _server.PYMUPDF_AVAILABLE = True
            _server.fitz = mock_fitz
            result = _server.list_embedded_files(b"%PDF-1.4 fake")

        names = [entry["name"] for entry in result]
        assert "factur-x.xml" not in names
        assert "attachment.xlsx" in names

    def test_excludes_zugferd_invoice_xml(self) -> None:
        """ZUGFeRD-invoice.xml wird NICHT zurückgegeben."""
        mock_fitz = _make_mock_fitz_with_xmp(
            metadata={},
            embfile_names=["ZUGFeRD-invoice.xml"],
            embfile_info_map={"ZUGFeRD-invoice.xml": {"size": 2000, "desc": ""}},
        )
        with patch.dict(sys.modules, {"fitz": mock_fitz}):
            _server.PYMUPDF_AVAILABLE = True
            _server.fitz = mock_fitz
            result = _server.list_embedded_files(b"%PDF-1.4 fake")

        assert result == []

    def test_excludes_xrechnung_xml(self) -> None:
        """xrechnung.xml wird NICHT zurückgegeben."""
        mock_fitz = _make_mock_fitz_with_xmp(
            metadata={},
            embfile_names=["xrechnung.xml", "logo.png"],
            embfile_info_map={
                "xrechnung.xml": {"size": 1500, "desc": ""},
                "logo.png": {"size": 4096, "desc": "Firmenlogo"},
            },
        )
        with patch.dict(sys.modules, {"fitz": mock_fitz}):
            _server.PYMUPDF_AVAILABLE = True
            _server.fitz = mock_fitz
            result = _server.list_embedded_files(b"%PDF-1.4 fake")

        names = [entry["name"] for entry in result]
        assert "xrechnung.xml" not in names
        assert "logo.png" in names

    def test_no_pymupdf_returns_empty_list(self) -> None:
        """Ohne PyMuPDF gibt list_embedded_files eine leere Liste zurück."""
        original = _server.PYMUPDF_AVAILABLE
        try:
            _server.PYMUPDF_AVAILABLE = False
            result = _server.list_embedded_files(b"%PDF-1.4 fake")
        finally:
            _server.PYMUPDF_AVAILABLE = original

        assert result == []


# ---------------------------------------------------------------------------
# Tests: Integration in convert_with_markitdown
# ---------------------------------------------------------------------------

class TestXmpInConvertWithMarkitdown:
    """xmp_metadata und embedded_files werden in convert_with_markitdown gesetzt."""

    def test_xmp_in_meta_result(self, tmp_path: Path) -> None:
        """
        Bei PDF-Konvertierung: result['xmp_metadata'] ist befüllt wenn XMP vorhanden.
        """
        pdf_path = tmp_path / "meta.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")

        mock_fitz = _make_mock_fitz_with_xmp(
            metadata={"title": "Test Dokument", "author": "Tester"},
            xmp_xml=SAMPLE_XMP_XML,
            embfile_names=[],
        )
        mock_md_result = MagicMock()
        mock_md_result.text_content = "# Test"
        mock_md_result.title = "Test"

        with patch.object(_server, "md") as mock_md_obj:
            mock_md_obj.convert = MagicMock(return_value=mock_md_result)
            with patch.object(_server, "PDFPLUMBER_AVAILABLE", False):
                with patch.dict(sys.modules, {"fitz": mock_fitz}):
                    _server.PYMUPDF_AVAILABLE = True
                    _server.fitz = mock_fitz
                    result = _server.convert_with_markitdown(pdf_path)

        assert result["success"] is True
        assert result.get("xmp_metadata") is not None
        assert result["xmp_metadata"]["title"] == "Test Dokument"

    def test_embedded_files_in_meta_result(self, tmp_path: Path) -> None:
        """
        Bei PDF-Konvertierung: result['embedded_files'] ist befüllt wenn Dateien vorhanden.
        """
        pdf_path = tmp_path / "with_attachment.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")

        mock_fitz = _make_mock_fitz_with_xmp(
            metadata={},
            xmp_xml="",
            embfile_names=["contract.docx"],
            embfile_info_map={"contract.docx": {"size": 8192, "desc": "Vertrag"}},
        )
        mock_md_result = MagicMock()
        mock_md_result.text_content = "# Test"
        mock_md_result.title = "Test"

        with patch.object(_server, "md") as mock_md_obj:
            mock_md_obj.convert = MagicMock(return_value=mock_md_result)
            with patch.object(_server, "PDFPLUMBER_AVAILABLE", False):
                with patch.dict(sys.modules, {"fitz": mock_fitz}):
                    _server.PYMUPDF_AVAILABLE = True
                    _server.fitz = mock_fitz
                    result = _server.convert_with_markitdown(pdf_path)

        assert result["success"] is True
        assert result.get("embedded_files") is not None
        assert len(result["embedded_files"]) == 1
        assert result["embedded_files"][0]["name"] == "contract.docx"

    def test_no_xmp_for_normal_pdf(self, tmp_path: Path) -> None:
        """PDF ohne Metadaten und ohne XMP: xmp_metadata ist None."""
        pdf_path = tmp_path / "plain.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")

        mock_fitz = _make_mock_fitz_with_xmp(metadata={}, xmp_xml="", embfile_names=[])
        mock_md_result = MagicMock()
        mock_md_result.text_content = "# Inhalt"
        mock_md_result.title = None

        with patch.object(_server, "md") as mock_md_obj:
            mock_md_obj.convert = MagicMock(return_value=mock_md_result)
            with patch.object(_server, "PDFPLUMBER_AVAILABLE", False):
                with patch.dict(sys.modules, {"fitz": mock_fitz}):
                    _server.PYMUPDF_AVAILABLE = True
                    _server.fitz = mock_fitz
                    result = _server.convert_with_markitdown(pdf_path)

        assert result["success"] is True
        assert result.get("xmp_metadata") is None
        assert result.get("embedded_files") is None


# ---------------------------------------------------------------------------
# Tests: Integration in convert_auto (über meta-Felder)
# ---------------------------------------------------------------------------

class TestXmpInConvertAuto:
    """xmp_metadata und embedded_files werden in convert_auto in meta propagiert."""

    def _make_convert_auto_patches(self, mock_result: dict):
        """Patches damit convert_auto den MarkItDown-Pfad nimmt."""
        return [
            patch.object(_server, "convert_with_markitdown", return_value=mock_result),
            patch.object(_server, "calculate_quality_score", return_value={}),
            patch.object(_server, "detect_mimetype_from_bytes", return_value="application/pdf"),
            patch.object(_server, "is_scanned_pdf", return_value=False),
        ]

    def test_xmp_in_meta(self) -> None:
        """Nach convert_auto: response.meta.xmp_metadata ist gesetzt wenn XMP vorhanden."""
        xmp_data = {"title": "Testvorgabe", "author": "Autor", "pdf_a_level": "3B"}
        mock_result = {
            "success": True,
            "markdown": "# Test",
            "title": None,
            "zugferd": None,
            "xmp_metadata": xmp_data,
            "embedded_files": None,
        }

        patches = self._make_convert_auto_patches(mock_result)
        with patches[0], patches[1], patches[2], patches[3]:
            response = run_async(_server.convert_auto(
                file_data=b"%PDF-1.4",
                filename="test.pdf",
                source="test.pdf",
                source_type="file",
                input_meta={},
            ))

        assert response.success is True
        assert response.meta.xmp_metadata is not None
        assert response.meta.xmp_metadata["pdf_a_level"] == "3B"

    def test_embedded_files_in_meta(self) -> None:
        """Nach convert_auto: response.meta.embedded_files ist gesetzt wenn Dateien vorhanden."""
        embedded_data = [{"name": "anhang.xlsx", "size": 5000}]
        mock_result = {
            "success": True,
            "markdown": "# Test",
            "title": None,
            "zugferd": None,
            "xmp_metadata": None,
            "embedded_files": embedded_data,
        }

        patches = self._make_convert_auto_patches(mock_result)
        with patches[0], patches[1], patches[2], patches[3]:
            response = run_async(_server.convert_auto(
                file_data=b"%PDF-1.4",
                filename="test.pdf",
                source="test.pdf",
                source_type="file",
                input_meta={},
            ))

        assert response.success is True
        assert response.meta.embedded_files is not None
        assert len(response.meta.embedded_files) == 1
        assert response.meta.embedded_files[0]["name"] == "anhang.xlsx"

    def test_no_xmp_when_none(self) -> None:
        """Ohne XMP: meta.xmp_metadata bleibt None."""
        mock_result = {
            "success": True,
            "markdown": "# Test",
            "title": None,
            "zugferd": None,
            "xmp_metadata": None,
            "embedded_files": None,
        }

        patches = self._make_convert_auto_patches(mock_result)
        with patches[0], patches[1], patches[2], patches[3]:
            response = run_async(_server.convert_auto(
                file_data=b"%PDF-1.4",
                filename="test.pdf",
                source="test.pdf",
                source_type="file",
                input_meta={},
            ))

        assert response.success is True
        assert response.meta.xmp_metadata is None
        assert response.meta.embedded_files is None


# ---------------------------------------------------------------------------
# Tests: Nicht-PDF-Dateien bekommen keine XMP-Extraktion
# ---------------------------------------------------------------------------

class TestNonPdfNoXmp:
    """Für Nicht-PDF-Dateien werden keine XMP/embedded_files-Felder gesetzt."""

    def test_docx_no_xmp_in_convert_with_markitdown(self, tmp_path: Path) -> None:
        """DOCX-Konvertierung: xmp_metadata und embedded_files sind nicht im Result."""
        docx_path = tmp_path / "test.docx"
        docx_path.write_bytes(b"PK fake docx content")

        mock_md_result = MagicMock()
        mock_md_result.text_content = "# DOCX Inhalt"
        mock_md_result.title = "DOCX"

        with patch.object(_server, "md") as mock_md_obj:
            mock_md_obj.convert = MagicMock(return_value=mock_md_result)
            with patch.object(_server, "extract_docx_extras", return_value={}):
                result = _server.convert_with_markitdown(docx_path)

        assert result["success"] is True
        # DOCX hat keine PDF-spezifischen Felder
        assert result.get("xmp_metadata") is None
        assert result.get("embedded_files") is None

    def test_non_pdf_no_xmp_in_convert_auto(self) -> None:
        """Für DOCX: meta.xmp_metadata und meta.embedded_files bleiben None."""
        mock_result = {
            "success": True,
            "markdown": "# DOCX Inhalt",
            "title": None,
            "zugferd": None,
            "xmp_metadata": None,
            "embedded_files": None,
        }

        with patch.object(_server, "convert_with_markitdown", return_value=mock_result), \
             patch.object(_server, "calculate_quality_score", return_value={}), \
             patch.object(_server, "detect_mimetype_from_bytes", return_value="application/vnd.openxmlformats-officedocument.wordprocessingml.document"), \
             patch.object(_server, "is_scanned_pdf", return_value=False):
            response = run_async(_server.convert_auto(
                file_data=b"PK fake docx",
                filename="test.docx",
                source="test.docx",
                source_type="file",
                input_meta={},
            ))

        assert response.success is True
        assert response.meta.xmp_metadata is None
        assert response.meta.embedded_files is None
