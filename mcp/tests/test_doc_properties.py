"""
Unit-Tests für T-MKIT-028: DOCX/XLSX/PPTX Document Properties extrahieren.

Tests prüfen:
- Core Properties: Author, Created, Modified aus docProps/core.xml
- App Properties: Company, Application aus docProps/app.xml
- Custom Properties: Beliebige Key-Value-Paare aus docProps/custom.xml
- Fehlende docProps-Dateien → leere Sections, kein Crash
- Nicht-Office-Dateien (PDF) → None
- meta.document_properties wird über convert_auto gesetzt
- XLSX Properties werden extrahiert
- PPTX Properties werden extrahiert
"""

import io
import sys
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from conftest import load_server_module, run_async


# =============================================================================
# Server-Modul laden
# =============================================================================

_server = load_server_module(use_real_pil=False)

extract_document_properties = _server.extract_document_properties
convert_auto = _server.convert_auto


# =============================================================================
# Hilfsfunktionen: Synthetische Office-ZIP-Archive bauen
# =============================================================================

CORE_XML_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties
    xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
    xmlns:dc="http://purl.org/dc/elements/1.1/"
    xmlns:dcterms="http://purl.org/dc/terms/"
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>{title}</dc:title>
  <dc:subject>{subject}</dc:subject>
  <dc:creator>{creator}</dc:creator>
  <cp:lastModifiedBy>{last_modified_by}</cp:lastModifiedBy>
  <dcterms:created xsi:type="dcterms:W3CDTF">{created}</dcterms:created>
  <dcterms:modified xsi:type="dcterms:W3CDTF">{modified}</dcterms:modified>
  <cp:revision>{revision}</cp:revision>
</cp:coreProperties>
"""

APP_XML_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">
  <Application>{application}</Application>
  <Company>{company}</Company>
  <Manager>{manager}</Manager>
  <Pages>{pages}</Pages>
  <Words>{words}</Words>
  <TotalTime>{total_time}</TotalTime>
</Properties>
"""

CUSTOM_XML_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"
    xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <property fmtid="{{D5CDD505-2E9C-101B-9397-08002B2CF9AE}}" pid="2" name="ProjectCode">
    <vt:lpwstr>PRJ-2026</vt:lpwstr>
  </property>
  <property fmtid="{{D5CDD505-2E9C-101B-9397-08002B2CF9AE}}" pid="3" name="Classification">
    <vt:lpwstr>Internal</vt:lpwstr>
  </property>
  <property fmtid="{{D5CDD505-2E9C-101B-9397-08002B2CF9AE}}" pid="4" name="Revision">
    <vt:i4>3</vt:i4>
  </property>
</Properties>
"""


def _build_office_zip(
    include_core: bool = True,
    include_app: bool = True,
    include_custom: bool = True,
    core_kwargs: dict | None = None,
    app_kwargs: dict | None = None,
) -> bytes:
    """Baut ein minimales Office-ZIP-Archiv mit den angegebenen docProps-Dateien."""
    core_defaults = {
        "title": "Test Document",
        "subject": "Testing",
        "creator": "Max Mustermann",
        "last_modified_by": "Erika Musterfrau",
        "created": "2024-01-15T10:30:00Z",
        "modified": "2024-06-20T14:45:00Z",
        "revision": "7",
    }
    app_defaults = {
        "application": "Microsoft Office Word",
        "company": "Musterfirma GmbH",
        "manager": "Hans Manager",
        "pages": "12",
        "words": "3500",
        "total_time": "42",
    }
    if core_kwargs:
        core_defaults.update(core_kwargs)
    if app_kwargs:
        app_defaults.update(app_kwargs)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # Minimal [Content_Types].xml so the ZIP is "office-like"
        zf.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        if include_core:
            zf.writestr("docProps/core.xml", CORE_XML_TEMPLATE.format(**core_defaults))
        if include_app:
            zf.writestr("docProps/app.xml", APP_XML_TEMPLATE.format(**app_defaults))
        if include_custom:
            zf.writestr("docProps/custom.xml", CUSTOM_XML_TEMPLATE)
    return buf.getvalue()


# =============================================================================
# Tests: Core Properties
# =============================================================================

class TestExtractCoreProperties:
    """test_extract_core_properties: Author, Created, Modified aus core.xml."""

    def test_author_extracted(self):
        """dc:creator wird als 'author' zurückgegeben."""
        data = _build_office_zip()
        result = extract_document_properties(data, "document.docx")
        assert result is not None
        assert result["core"]["author"] == "Max Mustermann"

    def test_created_extracted(self):
        """dcterms:created wird als 'created' zurückgegeben."""
        data = _build_office_zip()
        result = extract_document_properties(data, "document.docx")
        assert result is not None
        assert result["core"]["created"] == "2024-01-15T10:30:00Z"

    def test_modified_extracted(self):
        """dcterms:modified wird als 'modified' zurückgegeben."""
        data = _build_office_zip()
        result = extract_document_properties(data, "document.docx")
        assert result is not None
        assert result["core"]["modified"] == "2024-06-20T14:45:00Z"

    def test_title_extracted(self):
        """dc:title wird als 'title' zurückgegeben."""
        data = _build_office_zip()
        result = extract_document_properties(data, "document.docx")
        assert result is not None
        assert result["core"]["title"] == "Test Document"

    def test_revision_extracted(self):
        """cp:revision wird als 'revision' zurückgegeben."""
        data = _build_office_zip()
        result = extract_document_properties(data, "document.docx")
        assert result is not None
        assert result["core"]["revision"] == "7"

    def test_last_modified_by_extracted(self):
        """cp:lastModifiedBy wird als 'last_modified_by' zurückgegeben."""
        data = _build_office_zip()
        result = extract_document_properties(data, "document.docx")
        assert result is not None
        assert result["core"]["last_modified_by"] == "Erika Musterfrau"


# =============================================================================
# Tests: App Properties
# =============================================================================

class TestExtractAppProperties:
    """test_extract_app_properties: Company, Application aus app.xml."""

    def test_company_extracted(self):
        """Company wird aus app.xml extrahiert."""
        data = _build_office_zip()
        result = extract_document_properties(data, "document.docx")
        assert result is not None
        assert result["app"]["company"] == "Musterfirma GmbH"

    def test_application_extracted(self):
        """Application wird aus app.xml extrahiert."""
        data = _build_office_zip()
        result = extract_document_properties(data, "document.docx")
        assert result is not None
        assert result["app"]["application"] == "Microsoft Office Word"

    def test_pages_extracted(self):
        """Pages wird aus app.xml extrahiert."""
        data = _build_office_zip()
        result = extract_document_properties(data, "document.docx")
        assert result is not None
        assert result["app"]["pages"] == "12"

    def test_total_time_extracted(self):
        """TotalTime wird als 'total_time_minutes' extrahiert."""
        data = _build_office_zip()
        result = extract_document_properties(data, "document.docx")
        assert result is not None
        assert result["app"]["total_time_minutes"] == "42"

    def test_manager_extracted(self):
        """Manager wird aus app.xml extrahiert."""
        data = _build_office_zip()
        result = extract_document_properties(data, "document.docx")
        assert result is not None
        assert result["app"]["manager"] == "Hans Manager"


# =============================================================================
# Tests: Custom Properties
# =============================================================================

class TestExtractCustomProperties:
    """test_extract_custom_properties: Custom Key-Value Paare aus custom.xml."""

    def test_lpwstr_value_extracted(self):
        """lpwstr-Werte werden als Strings extrahiert."""
        data = _build_office_zip()
        result = extract_document_properties(data, "document.docx")
        assert result is not None
        assert result["custom"]["ProjectCode"] == "PRJ-2026"
        assert result["custom"]["Classification"] == "Internal"

    def test_i4_value_extracted(self):
        """i4 (integer) Werte werden als Strings extrahiert."""
        data = _build_office_zip()
        result = extract_document_properties(data, "document.docx")
        assert result is not None
        assert result["custom"]["Revision"] == "3"

    def test_multiple_custom_properties(self):
        """Mehrere Custom Properties werden alle extrahiert."""
        data = _build_office_zip()
        result = extract_document_properties(data, "document.docx")
        assert result is not None
        assert len(result["custom"]) >= 3


# =============================================================================
# Tests: Fehlende XML-Dateien → graceful leere Sections
# =============================================================================

class TestMissingXmlGraceful:
    """test_missing_xml_graceful: Fehlende docProps → leere Sections, kein Crash."""

    def test_missing_core_xml_returns_empty_core(self):
        """Fehlendes core.xml → core ist leeres Dict, kein Crash."""
        data = _build_office_zip(include_core=False)
        result = extract_document_properties(data, "document.docx")
        assert result is not None
        assert result["core"] == {}
        # app and custom still work
        assert result["app"]["company"] == "Musterfirma GmbH"

    def test_missing_app_xml_returns_empty_app(self):
        """Fehlendes app.xml → app ist leeres Dict, kein Crash."""
        data = _build_office_zip(include_app=False)
        result = extract_document_properties(data, "document.docx")
        assert result is not None
        assert result["app"] == {}
        # core still works
        assert result["core"]["author"] == "Max Mustermann"

    def test_missing_custom_xml_returns_empty_custom(self):
        """Fehlendes custom.xml → custom ist leeres Dict, kein Crash."""
        data = _build_office_zip(include_custom=False)
        result = extract_document_properties(data, "document.docx")
        assert result is not None
        assert result["custom"] == {}

    def test_no_docprops_at_all_returns_all_empty(self):
        """Keine einzige docProps-Datei → alle Sections leer, kein Crash."""
        data = _build_office_zip(include_core=False, include_app=False, include_custom=False)
        result = extract_document_properties(data, "document.docx")
        assert result is not None
        assert result["core"] == {}
        assert result["app"] == {}
        assert result["custom"] == {}


# =============================================================================
# Tests: Nicht-Office-Dateien → None
# =============================================================================

class TestNonOfficeFileReturnsNone:
    """test_non_office_file_returns_none: PDF → None."""

    def test_pdf_returns_none(self):
        """PDF-Extension → None."""
        result = extract_document_properties(b"%PDF fake data", "document.pdf")
        assert result is None

    def test_jpg_returns_none(self):
        """JPG-Extension → None."""
        result = extract_document_properties(b"\xff\xd8 fake jpeg", "photo.jpg")
        assert result is None

    def test_txt_returns_none(self):
        """TXT-Extension → None."""
        result = extract_document_properties(b"plain text", "readme.txt")
        assert result is None

    def test_csv_returns_none(self):
        """CSV-Extension → None."""
        result = extract_document_properties(b"a,b,c\n1,2,3", "data.csv")
        assert result is None


# =============================================================================
# Tests: meta.document_properties über convert_auto
# =============================================================================

class TestPropertiesInMeta:
    """test_properties_in_meta: meta.document_properties wird gesetzt."""

    def test_document_properties_set_in_meta_docx(self):
        """convert_auto setzt meta.document_properties für DOCX."""
        docx_data = _build_office_zip()

        with patch.object(_server, "detect_mimetype_from_bytes", return_value=None), \
             patch.object(_server, "convert_with_markitdown", return_value={
                 "success": True,
                 "markdown": "# Test",
                 "title": None,
                 "zugferd": None,
                 "xmp_metadata": None,
                 "embedded_files": None,
                 "document_properties": {
                     "core": {"author": "Max Mustermann", "created": "2024-01-15T10:30:00Z"},
                     "app": {"company": "Musterfirma GmbH"},
                     "custom": {"ProjectCode": "PRJ-2026"},
                 },
             }):
            response = run_async(convert_auto(
                file_data=docx_data,
                filename="document.docx",
                source="test",
                source_type="base64",
                input_meta={},
            ))

        assert response.success is True
        assert response.meta.document_properties is not None
        assert response.meta.document_properties["core"]["author"] == "Max Mustermann"

    def test_document_properties_none_when_not_office(self):
        """meta.document_properties ist None für Nicht-Office-Dateien."""
        with patch.object(_server, "detect_mimetype_from_bytes", return_value=None), \
             patch.object(_server, "convert_with_markitdown", return_value={
                 "success": True,
                 "markdown": "# Test",
                 "title": None,
                 "zugferd": None,
                 "xmp_metadata": None,
                 "embedded_files": None,
                 "document_properties": None,
             }):
            response = run_async(convert_auto(
                file_data=b"%PDF fake",
                filename="document.pdf",
                source="test",
                source_type="base64",
                input_meta={},
            ))

        assert response.success is True
        assert response.meta.document_properties is None


# =============================================================================
# Tests: XLSX Properties
# =============================================================================

class TestXlsxProperties:
    """test_xlsx_properties: Properties werden aus XLSX extrahiert."""

    def test_xlsx_core_author(self):
        """Excel-Dateien: Core Author wird extrahiert."""
        data = _build_office_zip(core_kwargs={"creator": "Excel User"})
        result = extract_document_properties(data, "report.xlsx")
        assert result is not None
        assert result["core"]["author"] == "Excel User"

    def test_xlsx_app_company(self):
        """Excel-Dateien: App Company wird extrahiert."""
        data = _build_office_zip(app_kwargs={"company": "Excel Corp"})
        result = extract_document_properties(data, "report.xlsx")
        assert result is not None
        assert result["app"]["company"] == "Excel Corp"

    def test_xlsx_custom_properties(self):
        """Excel-Dateien: Custom Properties werden extrahiert."""
        data = _build_office_zip()
        result = extract_document_properties(data, "report.xlsx")
        assert result is not None
        assert "ProjectCode" in result["custom"]


# =============================================================================
# Tests: PPTX Properties
# =============================================================================

class TestPptxProperties:
    """test_pptx_properties: Properties werden aus PPTX extrahiert."""

    def test_pptx_core_author(self):
        """PowerPoint-Dateien: Core Author wird extrahiert."""
        data = _build_office_zip(core_kwargs={"creator": "Presenter"})
        result = extract_document_properties(data, "slides.pptx")
        assert result is not None
        assert result["core"]["author"] == "Presenter"

    def test_pptx_app_company(self):
        """PowerPoint-Dateien: App Company wird extrahiert."""
        data = _build_office_zip(app_kwargs={"company": "Presenter GmbH"})
        result = extract_document_properties(data, "slides.pptx")
        assert result is not None
        assert result["app"]["company"] == "Presenter GmbH"

    def test_pptx_custom_properties(self):
        """PowerPoint-Dateien: Custom Properties werden extrahiert."""
        data = _build_office_zip()
        result = extract_document_properties(data, "slides.pptx")
        assert result is not None
        assert "Classification" in result["custom"]

    def test_pptx_not_none(self):
        """PPTX Extension wird als Office-Format erkannt (kein None)."""
        data = _build_office_zip()
        result = extract_document_properties(data, "slides.pptx")
        assert result is not None
