"""
Tests für T-DAI-012: OpenDocument Format-Extras (ODT, ODP, ODS).

Prüft die drei neuen Funktionen:
- extract_odt_extras: Kommentare, Track Changes, Header/Footer
- extract_odp_hidden_slides: Versteckte Slides
- convert_ods_enhanced: Multi-Sheet Markdown, Hidden Sheets, Formeln

Alle Tests laufen ohne Docker-Container und ohne echte API-Calls.
Alle Test-Dateien werden als in-memory ZIP-Dateien erzeugt.
"""

import io
import zipfile
from pathlib import Path

import pytest

from conftest import load_server_module


# =============================================================================
# ODF XML-Namespaces
# =============================================================================

OFFICE_NS = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"
TEXT_NS = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
DC_NS = "http://purl.org/dc/elements/1.1/"
STYLE_NS = "urn:oasis:names:tc:opendocument:xmlns:style:1.0"
TABLE_NS = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"
DRAW_NS = "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
PRESENTATION_NS = "urn:oasis:names:tc:opendocument:xmlns:presentation:1.0"


# =============================================================================
# Modul laden
# =============================================================================

_server = load_server_module(use_real_pil=False)

extract_odt_extras = _server.extract_odt_extras
extract_odp_hidden_slides = _server.extract_odp_hidden_slides
convert_ods_enhanced = _server.convert_ods_enhanced


# =============================================================================
# Hilfsfunktionen: Synthetische ODF-Dateien
# =============================================================================

def _make_odt_content_xml(comments=None, track_changes=None) -> bytes:
    """
    Erstellt ein minimales content.xml für eine ODT-Datei.

    Args:
        comments: Liste von Dicts mit 'author', 'date', 'text'
        track_changes: Liste von Dicts mit 'type' ('insertion'/'deletion'), 'author', 'date', 'text'

    Returns:
        UTF-8 kodierte XML-Bytes.
    """
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<office:document-content',
        f'  xmlns:office="{OFFICE_NS}"',
        f'  xmlns:text="{TEXT_NS}"',
        f'  xmlns:dc="{DC_NS}"',
        f'>',
        f'<office:body><office:text>',
        f'<text:p>Hauptinhalt des Dokuments.</text:p>',
    ]

    # Kommentare als office:annotation
    if comments:
        for c in comments:
            author = c.get("author", "")
            date = c.get("date", "")
            text = c.get("text", "")
            parts.append(
                f'<text:p>'
                f'<office:annotation>'
                f'<dc:creator>{author}</dc:creator>'
                f'<dc:date>{date}</dc:date>'
                f'<text:p>{text}</text:p>'
                f'</office:annotation>'
                f'</text:p>'
            )

    # Track Changes als text:tracked-changes
    if track_changes:
        parts.append(f'<text:tracked-changes>')
        for tc in track_changes:
            tc_type = tc.get("type", "insertion")
            author = tc.get("author", "")
            date = tc.get("date", "")
            text = tc.get("text", "")
            tag = "insertion" if tc_type == "insertion" else "deletion"
            parts.append(
                f'<text:{tag}'
                f' office:chng-author="{author}"'
                f' office:chng-date="{date}">'
                f'<text:p>{text}</text:p>'
                f'</text:{tag}>'
            )
        parts.append(f'</text:tracked-changes>')

    parts.append('</office:text></office:body></office:document-content>')
    return "\n".join(parts).encode("utf-8")


def _make_odt_styles_xml(headers=None, footers=None) -> bytes:
    """
    Erstellt ein minimales styles.xml für eine ODT-Datei.

    Args:
        headers: Liste von Header-Texten
        footers: Liste von Footer-Texten

    Returns:
        UTF-8 kodierte XML-Bytes.
    """
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<office:document-styles',
        f'  xmlns:office="{OFFICE_NS}"',
        f'  xmlns:style="{STYLE_NS}"',
        f'  xmlns:text="{TEXT_NS}"',
        f'>',
        f'<office:master-styles>',
        f'<style:master-page style:name="Standard">',
    ]

    if headers:
        for h_text in headers:
            parts.append(f'<style:header><text:p>{h_text}</text:p></style:header>')

    if footers:
        for f_text in footers:
            parts.append(f'<style:footer><text:p>{f_text}</text:p></style:footer>')

    parts.append('</style:master-page>')
    parts.append('</office:master-styles></office:document-styles>')
    return "\n".join(parts).encode("utf-8")


def _make_odt_zip(comments=None, track_changes=None, headers=None, footers=None) -> bytes:
    """Erstellt ein minimales ODT-ZIP mit content.xml und styles.xml."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("content.xml", _make_odt_content_xml(comments, track_changes))
        zf.writestr("styles.xml", _make_odt_styles_xml(headers, footers))
    return buf.getvalue()


def _make_odp_content_xml(slides) -> bytes:
    """
    Erstellt ein minimales content.xml für eine ODP-Datei.

    Args:
        slides: Liste von Dicts mit 'name' und optional 'hidden' (bool)

    Returns:
        UTF-8 kodierte XML-Bytes.
    """
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<office:document-content',
        f'  xmlns:office="{OFFICE_NS}"',
        f'  xmlns:draw="{DRAW_NS}"',
        f'  xmlns:presentation="{PRESENTATION_NS}"',
        f'  xmlns:style="{STYLE_NS}"',
        f'>',
        f'<office:automatic-styles>',
    ]

    # Stile für hidden slides
    for i, s in enumerate(slides):
        if s.get("hidden"):
            style_name = f"dp{i + 1}"
            parts.append(
                f'<style:style style:name="{style_name}" style:family="drawing-page">'
                f'<style:drawing-page-properties'
                f' presentation:visibility="hidden"/>'
                f'</style:style>'
            )

    parts.append('</office:automatic-styles>')
    parts.append('<office:body><office:presentation>')

    for i, s in enumerate(slides):
        slide_name = s.get("name", f"Slide{i + 1}")
        if s.get("hidden"):
            style_name = f"dp{i + 1}"
            parts.append(
                f'<draw:page draw:name="{slide_name}"'
                f' draw:style-name="{style_name}">'
                f'</draw:page>'
            )
        else:
            parts.append(
                f'<draw:page draw:name="{slide_name}">'
                f'</draw:page>'
            )

    parts.append('</office:presentation></office:body></office:document-content>')
    return "\n".join(parts).encode("utf-8")


def _make_odp_zip(slides) -> bytes:
    """Erstellt ein minimales ODP-ZIP mit content.xml."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("content.xml", _make_odp_content_xml(slides))
    return buf.getvalue()


def _make_ods_content_xml(sheets) -> bytes:
    """
    Erstellt ein minimales content.xml für eine ODS-Datei.

    Args:
        sheets: Liste von Dicts mit:
          - 'name': Sheet-Name
          - 'rows': Liste von Listen (Zeilendaten als Strings)
          - 'hidden': bool (optional)

    Returns:
        UTF-8 kodierte XML-Bytes.
    """
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<office:document-content',
        f'  xmlns:office="{OFFICE_NS}"',
        f'  xmlns:table="{TABLE_NS}"',
        f'  xmlns:text="{TEXT_NS}"',
        f'  xmlns:style="{STYLE_NS}"',
        f'>',
        f'<office:automatic-styles>',
    ]

    # Hidden Sheet Styles
    for i, sheet in enumerate(sheets):
        if sheet.get("hidden"):
            style_name = f"ta{i + 1}"
            parts.append(
                f'<style:style style:name="{style_name}" style:family="table">'
                f'<style:table-properties table:display="false"/>'
                f'</style:style>'
            )

    parts.append('</office:automatic-styles>')
    parts.append('<office:body><office:spreadsheet>')

    for i, sheet in enumerate(sheets):
        name = sheet.get("name", f"Tabelle{i + 1}")
        rows = sheet.get("rows", [])

        if sheet.get("hidden"):
            style_name = f"ta{i + 1}"
            parts.append(
                f'<table:table table:name="{name}" table:style-name="{style_name}">'
            )
        else:
            parts.append(f'<table:table table:name="{name}">')

        for row in rows:
            parts.append('<table:table-row>')
            for cell_val in row:
                parts.append(
                    f'<table:table-cell>'
                    f'<text:p>{cell_val}</text:p>'
                    f'</table:table-cell>'
                )
            parts.append('</table:table-row>')

        parts.append('</table:table>')

    parts.append('</office:spreadsheet></office:body></office:document-content>')
    return "\n".join(parts).encode("utf-8")


def _make_ods_zip(sheets) -> bytes:
    """Erstellt ein minimales ODS-ZIP mit content.xml."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("content.xml", _make_ods_content_xml(sheets))
    return buf.getvalue()


# =============================================================================
# Tests: extract_odt_extras
# =============================================================================

class TestExtractOdtExtras:
    """T-DAI-012: ODT Extras werden korrekt extrahiert."""

    def test_extract_comment_author_and_text(self, tmp_path: Path) -> None:
        """Kommentar mit Author und Text wird aus ODT extrahiert."""
        odt_bytes = _make_odt_zip(comments=[
            {"author": "Max Mustermann", "date": "2024-01-15T10:00:00", "text": "Sehr guter Punkt!"},
        ])
        odt_path = tmp_path / "test.odt"
        odt_path.write_bytes(odt_bytes)

        result = extract_odt_extras(odt_path)

        assert len(result["comments"]) == 1
        comment = result["comments"][0]
        assert comment["author"] == "Max Mustermann"
        assert comment["text"] == "Sehr guter Punkt!"
        assert comment["date"] == "2024-01-15T10:00:00"

    def test_extract_multiple_comments(self, tmp_path: Path) -> None:
        """Mehrere Kommentare werden alle extrahiert."""
        odt_bytes = _make_odt_zip(comments=[
            {"author": "Alice", "date": "2024-01-01T00:00:00", "text": "Kommentar A"},
            {"author": "Bob", "date": "2024-01-02T00:00:00", "text": "Kommentar B"},
        ])
        odt_path = tmp_path / "multi.odt"
        odt_path.write_bytes(odt_bytes)

        result = extract_odt_extras(odt_path)

        assert len(result["comments"]) == 2
        authors = {c["author"] for c in result["comments"]}
        assert authors == {"Alice", "Bob"}

    def test_no_comments_returns_empty_list(self, tmp_path: Path) -> None:
        """Kein Kommentar → leere Liste."""
        odt_bytes = _make_odt_zip()
        odt_path = tmp_path / "empty.odt"
        odt_path.write_bytes(odt_bytes)

        result = extract_odt_extras(odt_path)

        assert result["comments"] == []

    def test_extract_track_change_insertion(self, tmp_path: Path) -> None:
        """Track Change vom Typ 'insertion' wird erkannt."""
        odt_bytes = _make_odt_zip(track_changes=[
            {"type": "insertion", "author": "Alice", "date": "2024-01-01", "text": "neuer Text"},
        ])
        odt_path = tmp_path / "tc_ins.odt"
        odt_path.write_bytes(odt_bytes)

        result = extract_odt_extras(odt_path)

        assert len(result["track_changes"]) == 1
        tc = result["track_changes"][0]
        assert tc["type"] == "insertion"
        assert tc["author"] == "Alice"
        assert tc["text"] == "neuer Text"

    def test_extract_track_change_deletion(self, tmp_path: Path) -> None:
        """Track Change vom Typ 'deletion' wird erkannt."""
        odt_bytes = _make_odt_zip(track_changes=[
            {"type": "deletion", "author": "Bob", "date": "2024-01-02", "text": "alter Text"},
        ])
        odt_path = tmp_path / "tc_del.odt"
        odt_path.write_bytes(odt_bytes)

        result = extract_odt_extras(odt_path)

        assert len(result["track_changes"]) == 1
        tc = result["track_changes"][0]
        assert tc["type"] == "deletion"

    def test_extract_header_from_styles_xml(self, tmp_path: Path) -> None:
        """Header aus styles.xml wird extrahiert."""
        odt_bytes = _make_odt_zip(headers=["Firmenname GmbH"])
        odt_path = tmp_path / "header.odt"
        odt_path.write_bytes(odt_bytes)

        result = extract_odt_extras(odt_path)

        assert "Firmenname GmbH" in result["headers"]
        assert result["footers"] == []

    def test_extract_footer_from_styles_xml(self, tmp_path: Path) -> None:
        """Footer aus styles.xml wird extrahiert."""
        odt_bytes = _make_odt_zip(footers=["Seite 1 von 5"])
        odt_path = tmp_path / "footer.odt"
        odt_path.write_bytes(odt_bytes)

        result = extract_odt_extras(odt_path)

        assert "Seite 1 von 5" in result["footers"]
        assert result["headers"] == []

    def test_result_has_all_keys(self, tmp_path: Path) -> None:
        """Rückgabe-Dict enthält alle erwarteten Schlüssel."""
        odt_bytes = _make_odt_zip()
        odt_path = tmp_path / "keys.odt"
        odt_path.write_bytes(odt_bytes)

        result = extract_odt_extras(odt_path)

        assert "comments" in result
        assert "track_changes" in result
        assert "headers" in result
        assert "footers" in result

    def test_graceful_on_broken_zip(self, tmp_path: Path) -> None:
        """Kein Crash bei korrupter Datei."""
        bad_path = tmp_path / "bad.odt"
        bad_path.write_bytes(b"this is not a zip file")

        result = extract_odt_extras(bad_path)

        assert isinstance(result, dict)
        assert result["comments"] == []

    def test_no_duplicate_headers(self, tmp_path: Path) -> None:
        """Gleicher Header-Text darf nicht doppelt erscheinen."""
        # Beide Elemente im styles.xml mit gleichem Text (durch _make_odt_styles_xml)
        odt_bytes = _make_odt_zip(headers=["Header A", "Header A"])
        odt_path = tmp_path / "dup.odt"
        odt_path.write_bytes(odt_bytes)

        result = extract_odt_extras(odt_path)

        assert result["headers"].count("Header A") == 1


# =============================================================================
# Tests: extract_odp_hidden_slides
# =============================================================================

class TestExtractOdpHiddenSlides:
    """T-DAI-012: Versteckte Slides in ODP werden erkannt."""

    def test_no_hidden_slides_count_zero(self) -> None:
        """Alle Slides sichtbar → hidden_slide_count=0."""
        odp_data = _make_odp_zip([
            {"name": "Slide1", "hidden": False},
            {"name": "Slide2", "hidden": False},
        ])

        result = extract_odp_hidden_slides(odp_data)

        assert result is not None
        assert result["hidden_slide_count"] == 0
        assert result["hidden_slides"] == []

    def test_one_hidden_slide_detected(self) -> None:
        """Versteckter Slide wird erkannt."""
        odp_data = _make_odp_zip([
            {"name": "Visible", "hidden": False},
            {"name": "Hidden", "hidden": True},
            {"name": "Visible2", "hidden": False},
        ])

        result = extract_odp_hidden_slides(odp_data)

        assert result is not None
        assert result["hidden_slide_count"] == 1
        assert 2 in result["hidden_slides"]

    def test_multiple_hidden_slides(self) -> None:
        """Mehrere versteckte Slides werden alle erkannt."""
        odp_data = _make_odp_zip([
            {"name": "Slide1", "hidden": True},
            {"name": "Slide2", "hidden": False},
            {"name": "Slide3", "hidden": True},
        ])

        result = extract_odp_hidden_slides(odp_data)

        assert result is not None
        assert result["hidden_slide_count"] == 2
        assert 1 in result["hidden_slides"]
        assert 3 in result["hidden_slides"]

    def test_all_hidden_slides(self) -> None:
        """Alle Slides versteckt → alle in hidden_slides."""
        odp_data = _make_odp_zip([
            {"name": "S1", "hidden": True},
            {"name": "S2", "hidden": True},
        ])

        result = extract_odp_hidden_slides(odp_data)

        assert result is not None
        assert result["hidden_slide_count"] == 2

    def test_returns_none_on_broken_zip(self) -> None:
        """Kein Crash bei korruptem ZIP — gibt None zurück."""
        result = extract_odp_hidden_slides(b"not a zip")

        assert result is None

    def test_empty_presentation(self) -> None:
        """Leere Präsentation (keine Slides) → count=0."""
        odp_data = _make_odp_zip([])

        result = extract_odp_hidden_slides(odp_data)

        assert result is not None
        assert result["hidden_slide_count"] == 0

    def test_result_has_required_keys(self) -> None:
        """Rückgabe-Dict enthält hidden_slide_count und hidden_slides."""
        odp_data = _make_odp_zip([{"name": "S1", "hidden": False}])

        result = extract_odp_hidden_slides(odp_data)

        assert result is not None
        assert "hidden_slide_count" in result
        assert "hidden_slides" in result

    def test_hidden_slides_list_is_sorted(self) -> None:
        """hidden_slides Liste ist sortiert."""
        odp_data = _make_odp_zip([
            {"name": "S1", "hidden": False},
            {"name": "S2", "hidden": True},
            {"name": "S3", "hidden": True},
        ])

        result = extract_odp_hidden_slides(odp_data)

        assert result is not None
        assert result["hidden_slides"] == sorted(result["hidden_slides"])


# =============================================================================
# Tests: convert_ods_enhanced
# =============================================================================

class TestConvertOdsEnhanced:
    """T-DAI-012: ODS Multi-Sheet Konvertierung zu Markdown."""

    def test_single_sheet_basic(self, tmp_path: Path) -> None:
        """Einzel-Sheet wird korrekt zu Markdown konvertiert."""
        ods_bytes = _make_ods_zip([
            {"name": "Tabelle1", "rows": [["Name", "Wert"], ["Alice", "100"], ["Bob", "200"]]},
        ])
        ods_path = tmp_path / "test.ods"
        ods_path.write_bytes(ods_bytes)

        result = convert_ods_enhanced(ods_path)

        assert result["success"] is True
        assert "## Sheet: Tabelle1" in result["markdown"]
        assert "Alice" in result["markdown"]
        assert "Bob" in result["markdown"]

    def test_multi_sheet_each_has_heading(self, tmp_path: Path) -> None:
        """Zwei Sheets → zwei ## Sheet: Sektionen im Markdown."""
        ods_bytes = _make_ods_zip([
            {"name": "Sheet1", "rows": [["A", "B"], ["1", "2"]]},
            {"name": "Sheet2", "rows": [["X", "Y"], ["3", "4"]]},
        ])
        ods_path = tmp_path / "multi.ods"
        ods_path.write_bytes(ods_bytes)

        result = convert_ods_enhanced(ods_path)

        assert result["success"] is True
        assert "## Sheet: Sheet1" in result["markdown"]
        assert "## Sheet: Sheet2" in result["markdown"]
        assert result["sheets_count"] == 2

    def test_hidden_sheet_marked(self, tmp_path: Path) -> None:
        """Verstecktes Sheet bekommt [HIDDEN] im Heading."""
        ods_bytes = _make_ods_zip([
            {"name": "Sichtbar", "rows": [["Col1"], ["Wert1"]]},
            {"name": "Versteckt", "rows": [["Geheim"], ["Daten"]], "hidden": True},
        ])
        ods_path = tmp_path / "hidden.ods"
        ods_path.write_bytes(ods_bytes)

        result = convert_ods_enhanced(ods_path)

        assert result["success"] is True
        assert "## Sheet: Versteckt [HIDDEN]" in result["markdown"]
        assert "hidden_sheets" in result
        assert "Versteckt" in result["hidden_sheets"]

    def test_visible_sheet_no_hidden_key(self, tmp_path: Path) -> None:
        """Kein verstecktes Sheet → kein hidden_sheets Schlüssel."""
        ods_bytes = _make_ods_zip([
            {"name": "Normal", "rows": [["A"], ["1"]]},
        ])
        ods_path = tmp_path / "visible.ods"
        ods_path.write_bytes(ods_bytes)

        result = convert_ods_enhanced(ods_path)

        assert result["success"] is True
        assert "hidden_sheets" not in result

    def test_sheets_count_correct(self, tmp_path: Path) -> None:
        """sheets_count enthält die korrekte Anzahl."""
        ods_bytes = _make_ods_zip([
            {"name": "S1", "rows": [["A"], ["1"]]},
            {"name": "S2", "rows": [["B"], ["2"]]},
            {"name": "S3", "rows": [["C"], ["3"]]},
        ])
        ods_path = tmp_path / "three.ods"
        ods_path.write_bytes(ods_bytes)

        result = convert_ods_enhanced(ods_path)

        assert result["success"] is True
        assert result["sheets_count"] == 3

    def test_empty_sheet_graceful(self, tmp_path: Path) -> None:
        """Leeres Sheet führt nicht zu Crash."""
        ods_bytes = _make_ods_zip([
            {"name": "Leer", "rows": []},
        ])
        ods_path = tmp_path / "empty.ods"
        ods_path.write_bytes(ods_bytes)

        result = convert_ods_enhanced(ods_path)

        assert result["success"] is True
        assert "*Kein Inhalt*" in result["markdown"]

    def test_markdown_table_format(self, tmp_path: Path) -> None:
        """Markdown-Tabelle hat korrekte Pipes und Trennzeile."""
        ods_bytes = _make_ods_zip([
            {"name": "Data", "rows": [["Spalte1", "Spalte2"], ["Wert1", "Wert2"]]},
        ])
        ods_path = tmp_path / "table.ods"
        ods_path.write_bytes(ods_bytes)

        result = convert_ods_enhanced(ods_path)

        assert result["success"] is True
        assert "| Spalte1 | Spalte2 |" in result["markdown"]
        assert "| --- | --- |" in result["markdown"]
        assert "| Wert1 | Wert2 |" in result["markdown"]

    def test_broken_zip_returns_error(self, tmp_path: Path) -> None:
        """Kein Crash bei korrupter Datei — gibt Fehlerergebnis zurück."""
        bad_path = tmp_path / "bad.ods"
        bad_path.write_bytes(b"this is not a zip file")

        result = convert_ods_enhanced(bad_path)

        assert result["success"] is False
        assert "error" in result

    def test_no_content_xml_returns_error(self, tmp_path: Path) -> None:
        """ZIP ohne content.xml → Fehlerergebnis."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("mimetype", "application/vnd.oasis.opendocument.spreadsheet")
        bad_path = tmp_path / "nocontent.ods"
        bad_path.write_bytes(buf.getvalue())

        result = convert_ods_enhanced(bad_path)

        assert result["success"] is False

    def test_pipe_in_cell_escaped(self, tmp_path: Path) -> None:
        """Pipe-Zeichen in Zellen wird escapt um Markdown-Tabelle nicht zu brechen."""
        ods_bytes = _make_ods_zip([
            {"name": "Pipes", "rows": [["Header"], ["A|B"]]},
        ])
        ods_path = tmp_path / "pipes.ods"
        ods_path.write_bytes(ods_bytes)

        result = convert_ods_enhanced(ods_path)

        assert result["success"] is True
        assert "A\\|B" in result["markdown"]

    def test_multiple_hidden_sheets(self, tmp_path: Path) -> None:
        """Mehrere versteckte Sheets werden alle in hidden_sheets erfasst."""
        ods_bytes = _make_ods_zip([
            {"name": "Sichtbar", "rows": [["A"], ["1"]]},
            {"name": "Versteckt1", "rows": [["B"], ["2"]], "hidden": True},
            {"name": "Versteckt2", "rows": [["C"], ["3"]], "hidden": True},
        ])
        ods_path = tmp_path / "multi_hidden.ods"
        ods_path.write_bytes(ods_bytes)

        result = convert_ods_enhanced(ods_path)

        assert result["success"] is True
        assert "hidden_sheets" in result
        assert len(result["hidden_sheets"]) == 2
        assert "Versteckt1" in result["hidden_sheets"]
        assert "Versteckt2" in result["hidden_sheets"]
