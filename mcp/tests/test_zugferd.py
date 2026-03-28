"""
Tests für T-MKIT-024: ZUGFeRD/Factur-X E-Rechnung Extraktion.

Alle Tests laufen ohne Docker-Container und ohne echte PDF-Dateien.
PyMuPDF (fitz) wird vollständig gemockt.

Acceptance Criteria:
- Erkennung von factur-x.xml / ZUGFeRD-invoice.xml / xrechnung.xml in eingebetteten Anhängen
- Graceful fallback bei normalem PDF (None)
- Parsing von invoice_number, amounts, currency aus ZUGFeRD XML
- meta.zugferd wird bei PDF-Konvertierung befüllt
- Bei extract_schema + ZUGFeRD: extracted kommt aus ZUGFeRD, kein LLM
- Hint wenn ZUGFeRD erkannt aber kein template/extract_schema
- Graceful bei kaputtem XML
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
# Minimales ZUGFeRD-Test-XML
# ---------------------------------------------------------------------------

SAMPLE_ZUGFERD_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rsm:CrossIndustryInvoice xmlns:rsm="urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"
  xmlns:ram="urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100"
  xmlns:udt="urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100">
  <rsm:ExchangedDocument>
    <ram:ID>INV-2026-001</ram:ID>
    <ram:TypeCode>380</ram:TypeCode>
    <ram:IssueDateTime><udt:DateTimeString format="102">20260315</udt:DateTimeString></ram:IssueDateTime>
  </rsm:ExchangedDocument>
  <rsm:SupplyChainTradeTransaction>
    <ram:ApplicableHeaderTradeAgreement>
      <ram:SellerTradeParty>
        <ram:Name>Muster GmbH</ram:Name>
        <ram:PostalTradeAddress>
          <ram:LineOne>Musterstrasse 1</ram:LineOne>
          <ram:PostcodeCode>10115</ram:PostcodeCode>
          <ram:CityName>Berlin</ram:CityName>
          <ram:CountryID>DE</ram:CountryID>
        </ram:PostalTradeAddress>
        <ram:SpecifiedTaxRegistration>
          <ram:ID schemeID="VA">DE123456789</ram:ID>
        </ram:SpecifiedTaxRegistration>
      </ram:SellerTradeParty>
      <ram:BuyerTradeParty>
        <ram:Name>Kaeufer AG</ram:Name>
      </ram:BuyerTradeParty>
    </ram:ApplicableHeaderTradeAgreement>
    <ram:ApplicableHeaderTradeSettlement>
      <ram:InvoiceCurrencyCode>EUR</ram:InvoiceCurrencyCode>
      <ram:PaymentReference>REF-2026-001</ram:PaymentReference>
      <ram:SpecifiedTradeSettlementHeaderMonetarySummation>
        <ram:TaxBasisTotalAmount>1000.00</ram:TaxBasisTotalAmount>
        <ram:TaxTotalAmount currencyID="EUR">190.00</ram:TaxTotalAmount>
        <ram:GrandTotalAmount>1190.00</ram:GrandTotalAmount>
        <ram:DuePayableAmount>1190.00</ram:DuePayableAmount>
      </ram:SpecifiedTradeSettlementHeaderMonetarySummation>
    </ram:ApplicableHeaderTradeSettlement>
  </rsm:SupplyChainTradeTransaction>
</rsm:CrossIndustryInvoice>""".encode("utf-8")

INVALID_XML = b"this is not xml at all <<<not_valid>>>"


# ---------------------------------------------------------------------------
# Hilfsfunktionen: Mock für PyMuPDF embfile_names/embfile_get
# ---------------------------------------------------------------------------

def _make_mock_fitz_with_embfile(embfile_name: str, xml_content: bytes) -> MagicMock:
    """Erstellt einen Mock für fitz mit eingebettetem XML."""
    mock_doc = MagicMock()
    mock_doc.embfile_names = MagicMock(return_value=[embfile_name])
    mock_doc.embfile_get = MagicMock(return_value={"content": xml_content})
    mock_doc.close = MagicMock()

    mock_fitz = MagicMock()
    mock_fitz.open = MagicMock(return_value=mock_doc)
    return mock_fitz


def _make_mock_fitz_no_embfiles() -> MagicMock:
    """Erstellt einen Mock für fitz ohne eingebettete Dateien."""
    mock_doc = MagicMock()
    mock_doc.embfile_names = MagicMock(return_value=[])
    mock_doc.embfile_get = MagicMock(return_value=None)
    mock_doc.close = MagicMock()

    mock_fitz = MagicMock()
    mock_fitz.open = MagicMock(return_value=mock_doc)
    return mock_fitz


# ---------------------------------------------------------------------------
# Tests: detect_zugferd — Erkennung
# ---------------------------------------------------------------------------

class TestDetectZugferd:
    """Tests für detect_zugferd() Funktion."""

    def test_detect_zugferd_facturx(self) -> None:
        """factur-x.xml wird als ZUGFeRD/Factur-X 2.x erkannt."""
        mock_fitz = _make_mock_fitz_with_embfile("factur-x.xml", SAMPLE_ZUGFERD_XML)

        with patch.dict(sys.modules, {"fitz": mock_fitz}):
            _server.PYMUPDF_AVAILABLE = True
            _server.fitz = mock_fitz
            result = _server.detect_zugferd(b"%PDF-1.4 fake")

        assert result is not None
        assert result == SAMPLE_ZUGFERD_XML

    def test_detect_zugferd_v1(self) -> None:
        """ZUGFeRD-invoice.xml wird als ZUGFeRD 1.x erkannt."""
        mock_fitz = _make_mock_fitz_with_embfile("ZUGFeRD-invoice.xml", SAMPLE_ZUGFERD_XML)

        with patch.dict(sys.modules, {"fitz": mock_fitz}):
            _server.PYMUPDF_AVAILABLE = True
            _server.fitz = mock_fitz
            result = _server.detect_zugferd(b"%PDF-1.4 fake")

        assert result is not None
        assert result == SAMPLE_ZUGFERD_XML

    def test_detect_zugferd_xrechnung(self) -> None:
        """xrechnung.xml wird als ZUGFeRD-kompatibles Format erkannt."""
        mock_fitz = _make_mock_fitz_with_embfile("xrechnung.xml", SAMPLE_ZUGFERD_XML)

        with patch.dict(sys.modules, {"fitz": mock_fitz}):
            _server.PYMUPDF_AVAILABLE = True
            _server.fitz = mock_fitz
            result = _server.detect_zugferd(b"%PDF-1.4 fake")

        assert result is not None

    def test_detect_no_zugferd(self) -> None:
        """Normales PDF ohne eingebettetes XML → None."""
        mock_fitz = _make_mock_fitz_no_embfiles()

        with patch.dict(sys.modules, {"fitz": mock_fitz}):
            _server.PYMUPDF_AVAILABLE = True
            _server.fitz = mock_fitz
            result = _server.detect_zugferd(b"%PDF-1.4 fake")

        assert result is None

    def test_detect_zugferd_other_embedded_file_ignored(self) -> None:
        """Eingebettete Datei mit unbekanntem Namen wird ignoriert → None."""
        mock_fitz = _make_mock_fitz_with_embfile("attachment.pdf", b"some content")

        with patch.dict(sys.modules, {"fitz": mock_fitz}):
            _server.PYMUPDF_AVAILABLE = True
            _server.fitz = mock_fitz
            result = _server.detect_zugferd(b"%PDF-1.4 fake")

        assert result is None

    def test_detect_zugferd_no_pymupdf(self) -> None:
        """Ohne PyMuPDF gibt detect_zugferd None zurück."""
        original = _server.PYMUPDF_AVAILABLE
        try:
            _server.PYMUPDF_AVAILABLE = False
            result = _server.detect_zugferd(b"%PDF-1.4 fake")
        finally:
            _server.PYMUPDF_AVAILABLE = original

        assert result is None

    def test_detect_zugferd_graceful_on_fitz_error(self) -> None:
        """Wenn fitz.open() eine Exception wirft, wird None zurückgegeben."""
        mock_fitz = MagicMock()
        mock_fitz.open = MagicMock(side_effect=Exception("corrupt PDF"))

        with patch.dict(sys.modules, {"fitz": mock_fitz}):
            _server.PYMUPDF_AVAILABLE = True
            _server.fitz = mock_fitz
            result = _server.detect_zugferd(b"not a pdf")

        assert result is None

    def test_detect_zugferd_case_insensitive(self) -> None:
        """Dateinamen-Erkennung ist case-insensitiv (factur-x.xml vs FACTUR-X.XML)."""
        mock_fitz = _make_mock_fitz_with_embfile("FACTUR-X.XML", SAMPLE_ZUGFERD_XML)

        with patch.dict(sys.modules, {"fitz": mock_fitz}):
            _server.PYMUPDF_AVAILABLE = True
            _server.fitz = mock_fitz
            result = _server.detect_zugferd(b"%PDF-1.4 fake")

        assert result is not None


# ---------------------------------------------------------------------------
# Tests: parse_zugferd_xml — Parsing
# ---------------------------------------------------------------------------

class TestParseZugferdXml:
    """Tests für parse_zugferd_xml() Funktion."""

    def test_parse_zugferd_invoice_number(self) -> None:
        """invoice_number (BT-1) wird korrekt extrahiert."""
        result = _server.parse_zugferd_xml(SAMPLE_ZUGFERD_XML)
        assert result["invoice_number"] == "INV-2026-001"

    def test_parse_zugferd_invoice_type(self) -> None:
        """invoice_type (BT-3) wird korrekt extrahiert (380 = Rechnung)."""
        result = _server.parse_zugferd_xml(SAMPLE_ZUGFERD_XML)
        assert result["invoice_type"] == "380"

    def test_parse_zugferd_invoice_date(self) -> None:
        """invoice_date (BT-2) wird als ISO-8601-Datum formatiert."""
        result = _server.parse_zugferd_xml(SAMPLE_ZUGFERD_XML)
        assert result["invoice_date"] == "2026-03-15"

    def test_parse_zugferd_amounts(self) -> None:
        """Beträge (BT-109, BT-110, BT-112, BT-115) werden korrekt extrahiert."""
        result = _server.parse_zugferd_xml(SAMPLE_ZUGFERD_XML)
        assert result["total_net"] == "1000.00"
        assert result["total_vat"] == "190.00"
        assert result["total_gross"] == "1190.00"
        assert result["due_amount"] == "1190.00"

    def test_parse_zugferd_currency(self) -> None:
        """currency (BT-5) wird korrekt extrahiert."""
        result = _server.parse_zugferd_xml(SAMPLE_ZUGFERD_XML)
        assert result["currency"] == "EUR"

    def test_parse_zugferd_seller_name(self) -> None:
        """seller_name (BT-27) wird korrekt extrahiert."""
        result = _server.parse_zugferd_xml(SAMPLE_ZUGFERD_XML)
        assert result["seller_name"] == "Muster GmbH"

    def test_parse_zugferd_buyer_name(self) -> None:
        """buyer_name (BT-44) wird korrekt extrahiert."""
        result = _server.parse_zugferd_xml(SAMPLE_ZUGFERD_XML)
        assert result["buyer_name"] == "Kaeufer AG"

    def test_parse_zugferd_seller_vat_id(self) -> None:
        """seller_vat_id (BT-31, schemeID=VA) wird korrekt extrahiert."""
        result = _server.parse_zugferd_xml(SAMPLE_ZUGFERD_XML)
        assert result["seller_vat_id"] == "DE123456789"

    def test_parse_zugferd_payment_reference(self) -> None:
        """payment_reference (BT-83) wird korrekt extrahiert."""
        result = _server.parse_zugferd_xml(SAMPLE_ZUGFERD_XML)
        assert result["payment_reference"] == "REF-2026-001"

    def test_parse_zugferd_line_items_empty(self) -> None:
        """Ohne Positionen ist line_items eine leere Liste."""
        result = _server.parse_zugferd_xml(SAMPLE_ZUGFERD_XML)
        assert isinstance(result["line_items"], list)
        assert result["line_items"] == []

    def test_parse_zugferd_line_items_present(self) -> None:
        """Mit Positionen werden line_items korrekt extrahiert."""
        xml_with_items = b"""<?xml version="1.0" encoding="UTF-8"?>
<rsm:CrossIndustryInvoice xmlns:rsm="urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"
  xmlns:ram="urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100"
  xmlns:udt="urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100">
  <rsm:ExchangedDocument>
    <ram:ID>INV-001</ram:ID>
    <ram:TypeCode>380</ram:TypeCode>
  </rsm:ExchangedDocument>
  <rsm:SupplyChainTradeTransaction>
    <ram:IncludedSupplyChainTradeLineItem>
      <ram:SpecifiedTradeProduct>
        <ram:Name>Beratungsleistung</ram:Name>
      </ram:SpecifiedTradeProduct>
      <ram:SpecifiedLineTradeAgreement>
        <ram:GrossPriceProductTradePrice>
          <ram:ChargeAmount>100.00</ram:ChargeAmount>
        </ram:GrossPriceProductTradePrice>
      </ram:SpecifiedLineTradeAgreement>
      <ram:SpecifiedLineTradeDelivery>
        <ram:BilledQuantity>10</ram:BilledQuantity>
      </ram:SpecifiedLineTradeDelivery>
      <ram:SpecifiedLineTradeSettlement>
        <ram:ApplicableTradeTax>
          <ram:RateApplicablePercent>19</ram:RateApplicablePercent>
        </ram:ApplicableTradeTax>
        <ram:SpecifiedTradeSettlementLineMonetarySummation>
          <ram:LineTotalAmount>1000.00</ram:LineTotalAmount>
        </ram:SpecifiedTradeSettlementLineMonetarySummation>
      </ram:SpecifiedLineTradeSettlement>
    </ram:IncludedSupplyChainTradeLineItem>
  </rsm:SupplyChainTradeTransaction>
</rsm:CrossIndustryInvoice>"""
        result = _server.parse_zugferd_xml(xml_with_items)
        assert len(result["line_items"]) == 1
        item = result["line_items"][0]
        assert item["description"] == "Beratungsleistung"
        assert item["vat_rate"] == "19"
        assert item["total"] == "1000.00"

    def test_parse_zugferd_invalid_xml(self) -> None:
        """Kaputtes XML wird graceful behandelt — parse_error Feld gesetzt."""
        result = _server.parse_zugferd_xml(INVALID_XML)
        assert "parse_error" in result
        # Alle Pflichtfelder sind trotzdem vorhanden (mit None)
        assert "invoice_number" in result
        assert "currency" in result

    def test_parse_zugferd_returns_dict(self) -> None:
        """parse_zugferd_xml gibt immer ein Dict zurück."""
        result = _server.parse_zugferd_xml(SAMPLE_ZUGFERD_XML)
        assert isinstance(result, dict)

    def test_parse_zugferd_seller_address(self) -> None:
        """seller_address wird aus Adressfeldern zusammengebaut."""
        result = _server.parse_zugferd_xml(SAMPLE_ZUGFERD_XML)
        assert result["seller_address"] is not None
        assert "Berlin" in result["seller_address"]


# ---------------------------------------------------------------------------
# Tests: Integration in convert_with_markitdown
# ---------------------------------------------------------------------------

class TestZugferdInConvertWithMarkitdown:
    """ZUGFeRD wird nach markitdown-Konvertierung für .pdf erkannt."""

    def test_zugferd_in_meta_result(self, tmp_path: Path) -> None:
        """
        Bei .pdf-Konvertierung mit ZUGFeRD: result['zugferd'] ist befüllt.
        """
        pdf_path = tmp_path / "zugferd.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")

        mock_fitz = _make_mock_fitz_with_embfile("factur-x.xml", SAMPLE_ZUGFERD_XML)

        mock_md_result = MagicMock()
        mock_md_result.text_content = "# Rechnung"
        mock_md_result.title = "Rechnung"

        with patch.object(_server, "md") as mock_md_obj:
            mock_md_obj.convert = MagicMock(return_value=mock_md_result)
            with patch.object(_server, "PDFPLUMBER_AVAILABLE", False):
                with patch.dict(sys.modules, {"fitz": mock_fitz}):
                    _server.PYMUPDF_AVAILABLE = True
                    _server.fitz = mock_fitz
                    result = _server.convert_with_markitdown(pdf_path)

        assert result["success"] is True
        assert result["zugferd"] is not None
        assert result["zugferd"]["invoice_number"] == "INV-2026-001"

    def test_no_zugferd_in_normal_pdf(self, tmp_path: Path) -> None:
        """Normales PDF ohne ZUGFeRD: result['zugferd'] ist None."""
        pdf_path = tmp_path / "normal.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")

        mock_fitz = _make_mock_fitz_no_embfiles()

        mock_md_result = MagicMock()
        mock_md_result.text_content = "# Dokument"
        mock_md_result.title = "Dokument"

        with patch.object(_server, "md") as mock_md_obj:
            mock_md_obj.convert = MagicMock(return_value=mock_md_result)
            with patch.object(_server, "PDFPLUMBER_AVAILABLE", False):
                with patch.dict(sys.modules, {"fitz": mock_fitz}):
                    _server.PYMUPDF_AVAILABLE = True
                    _server.fitz = mock_fitz
                    result = _server.convert_with_markitdown(pdf_path)

        assert result["success"] is True
        assert result.get("zugferd") is None

    def test_zugferd_error_does_not_break_conversion(self, tmp_path: Path) -> None:
        """Wenn ZUGFeRD-Extraktion fehlschlägt, bleibt die Konvertierung erfolgreich."""
        pdf_path = tmp_path / "error.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")

        mock_md_result = MagicMock()
        mock_md_result.text_content = "# Inhalt"
        mock_md_result.title = "Test"

        with patch.object(_server, "md") as mock_md_obj:
            mock_md_obj.convert = MagicMock(return_value=mock_md_result)
            with patch.object(_server, "PDFPLUMBER_AVAILABLE", False):
                with patch.object(_server, "detect_zugferd", side_effect=Exception("fitz error")):
                    result = _server.convert_with_markitdown(pdf_path)

        assert result["success"] is True


# ---------------------------------------------------------------------------
# Tests: Integration in convert_auto (via meta und extracted)
# ---------------------------------------------------------------------------

class TestZugferdInConvertAuto:
    """ZUGFeRD wird in convert_auto in meta.zugferd und extracted propagiert."""

    def _make_markitdown_patches(self, mock_result, extra_patches=None):
        """
        Erstellt die notwendigen Patches damit convert_auto den MarkItDown-Pfad
        (nicht Vision) für PDF-Dateien nimmt.

        Notwendig weil:
        - detect_mimetype_from_bytes mit gemocktem 'magic' sonst MagicMock zurückgibt
        - MagicMock.startswith("image/") ist truthy → Vision-Pfad würde gewählt
        - is_scanned_pdf muss False zurückgeben
        """
        patches = [
            patch.object(_server, "convert_with_markitdown", return_value=mock_result),
            patch.object(_server, "calculate_quality_score", return_value={}),
            patch.object(_server, "detect_mimetype_from_bytes", return_value="application/pdf"),
            patch.object(_server, "is_scanned_pdf", return_value=False),
        ]
        if extra_patches:
            patches.extend(extra_patches)
        return patches

    def test_zugferd_in_meta_zugferd_field(self) -> None:
        """
        Nach convert_auto: response.meta.zugferd ist befüllt wenn ZUGFeRD gefunden.
        """
        zugferd_data = {"invoice_number": "INV-2026-001", "currency": "EUR", "line_items": []}
        mock_result = {
            "success": True,
            "markdown": "# Rechnung",
            "title": None,
            "zugferd": zugferd_data,
        }

        with patch.object(_server, "convert_with_markitdown", return_value=mock_result), \
             patch.object(_server, "calculate_quality_score", return_value={}), \
             patch.object(_server, "detect_mimetype_from_bytes", return_value="application/pdf"), \
             patch.object(_server, "is_scanned_pdf", return_value=False):
            response = run_async(_server.convert_auto(
                file_data=b"%PDF-1.4",
                filename="rechnung.pdf",
                source="rechnung.pdf",
                source_type="file",
                input_meta={},
            ))

        assert response.success is True
        assert response.meta.zugferd is not None
        assert response.meta.zugferd["invoice_number"] == "INV-2026-001"

    def test_zugferd_hint_when_no_template(self) -> None:
        """
        Wenn ZUGFeRD erkannt aber kein extract_schema/template gesetzt:
        Hint wird in meta.hints eingefügt.
        """
        zugferd_data = {"invoice_number": "INV-001", "currency": "EUR", "line_items": []}
        mock_result = {
            "success": True,
            "markdown": "# Rechnung",
            "title": None,
            "zugferd": zugferd_data,
        }

        with patch.object(_server, "convert_with_markitdown", return_value=mock_result), \
             patch.object(_server, "calculate_quality_score", return_value={}), \
             patch.object(_server, "detect_mimetype_from_bytes", return_value="application/pdf"), \
             patch.object(_server, "is_scanned_pdf", return_value=False):
            response = run_async(_server.convert_auto(
                file_data=b"%PDF-1.4",
                filename="rechnung.pdf",
                source="rechnung.pdf",
                source_type="file",
                input_meta={},
                extract_schema=None,
            ))

        assert response.meta.hints is not None
        zugferd_hints = [h for h in response.meta.hints if "ZUGFeRD" in h or "Factur-X" in h]
        assert len(zugferd_hints) > 0

    def test_zugferd_with_invoice_template_uses_zugferd_not_llm(self) -> None:
        """
        Wenn ZUGFeRD erkannt UND extract_schema gesetzt:
        extracted kommt aus ZUGFeRD (kein LLM-Call).
        """
        zugferd_data = {
            "invoice_number": "INV-2026-001",
            "currency": "EUR",
            "total_gross": "1190.00",
            "line_items": [],
        }
        mock_result = {
            "success": True,
            "markdown": "# Rechnung",
            "title": None,
            "zugferd": zugferd_data,
        }
        invoice_schema = {"type": "object", "properties": {"invoice_number": {"type": "string"}}}

        with patch.object(_server, "convert_with_markitdown", return_value=mock_result), \
             patch.object(_server, "calculate_quality_score", return_value={}), \
             patch.object(_server, "detect_mimetype_from_bytes", return_value="application/pdf"), \
             patch.object(_server, "is_scanned_pdf", return_value=False), \
             patch.object(_server, "extract_structured_data") as mock_extract:
            response = run_async(_server.convert_auto(
                file_data=b"%PDF-1.4",
                filename="rechnung.pdf",
                source="rechnung.pdf",
                source_type="file",
                input_meta={},
                extract_schema=invoice_schema,
            ))
            # LLM-Extraktion darf NICHT aufgerufen worden sein
            mock_extract.assert_not_called()

        assert response.success is True
        assert response.extracted is not None
        assert response.extracted["invoice_number"] == "INV-2026-001"
        assert response.meta.zugferd_source == "embedded_xml"
        assert response.meta.extraction_method == "zugferd"

    def test_no_hint_when_no_zugferd(self) -> None:
        """Ohne ZUGFeRD: Kein ZUGFeRD-Hint in meta.hints."""
        mock_result = {
            "success": True,
            "markdown": "# Normales Dokument",
            "title": None,
            "zugferd": None,
        }

        with patch.object(_server, "convert_with_markitdown", return_value=mock_result), \
             patch.object(_server, "calculate_quality_score", return_value={}), \
             patch.object(_server, "detect_mimetype_from_bytes", return_value="application/pdf"), \
             patch.object(_server, "is_scanned_pdf", return_value=False):
            response = run_async(_server.convert_auto(
                file_data=b"%PDF-1.4",
                filename="normal.pdf",
                source="normal.pdf",
                source_type="file",
                input_meta={},
            ))

        if response.meta.hints:
            zugferd_hints = [h for h in response.meta.hints if "ZUGFeRD" in h]
            assert len(zugferd_hints) == 0

    def test_without_zugferd_extract_schema_uses_llm(self) -> None:
        """
        Ohne ZUGFeRD UND mit extract_schema: LLM-Extraktion wird aufgerufen.
        """
        mock_result = {
            "success": True,
            "markdown": "# Rechnung ohne ZUGFeRD",
            "title": None,
            "zugferd": None,
        }
        invoice_schema = {"type": "object", "properties": {"invoice_number": {"type": "string"}}}

        with patch.object(_server, "convert_with_markitdown", return_value=mock_result), \
             patch.object(_server, "calculate_quality_score", return_value={}), \
             patch.object(_server, "detect_mimetype_from_bytes", return_value="application/pdf"), \
             patch.object(_server, "is_scanned_pdf", return_value=False), \
             patch.object(
                 _server,
                 "extract_structured_data",
                 return_value={"success": True, "extracted": {"invoice_number": "INV-999"}},
             ) as mock_extract:
            response = run_async(_server.convert_auto(
                file_data=b"%PDF-1.4",
                filename="rechnung.pdf",
                source="rechnung.pdf",
                source_type="file",
                input_meta={},
                extract_schema=invoice_schema,
            ))
            # LLM-Extraktion MUSS aufgerufen worden sein
            mock_extract.assert_called_once()

        assert response.success is True
        assert response.extracted["invoice_number"] == "INV-999"
