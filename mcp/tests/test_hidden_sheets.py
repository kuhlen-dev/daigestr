"""
Unit-Tests für T-MKIT-026: XLSX Hidden/VeryHidden Sheets extrahieren.

Tests prüfen:
- Sichtbare Sheets werden ohne Annotation ausgegeben
- Hidden Sheets werden mit [HIDDEN] markiert
- VeryHidden Sheets werden mit [VERY HIDDEN] markiert
- Hidden Sheets erscheinen NACH sichtbaren Sheets
- meta.hidden_sheets wird korrekt gesetzt
- Keine Hidden Sheets → meta.hidden_sheets ist None
- Gemischte Sichtbarkeit (visible + hidden + veryHidden)
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from conftest import load_server_module, run_async


# =============================================================================
# Modul-Setup mit openpyxl-Mock
# =============================================================================

def _make_openpyxl_mock():
    """Erstellt einen vollständigen openpyxl-Mock der sicher importiert werden kann."""
    openpyxl_mod = MagicMock()
    openpyxl_utils_mod = MagicMock()
    openpyxl_utils_mod.get_column_letter = lambda i: chr(ord("A") + i - 1)
    openpyxl_mod.utils = openpyxl_utils_mod
    openpyxl_mod.load_workbook = MagicMock(return_value=MagicMock())
    return openpyxl_mod, openpyxl_utils_mod


_openpyxl_mock, _openpyxl_utils_mock = _make_openpyxl_mock()

_server = load_server_module(
    use_real_pil=False,
    extra_patches={
        "openpyxl": _openpyxl_mock,
        "openpyxl.utils": _openpyxl_utils_mock,
    },
)

convert_excel_enhanced = _server.convert_excel_enhanced
convert_auto = _server.convert_auto


# =============================================================================
# Hilfsfunktionen für Mock-Workbooks mit sheet_state
# =============================================================================

def _make_cell(value=None):
    """Erstellt eine Mock-Zelle."""
    cell = MagicMock()
    cell.value = value
    return cell


def _make_worksheet_with_state(name: str, data: list[list], state: str = "visible"):
    """
    Erstellt ein Mock-Worksheet mit sheet_state und einfachen Daten.

    Args:
        name: Sheet-Name
        data: 2D-Liste mit Zellwerten
        state: "visible", "hidden", oder "veryHidden"
    """
    rows_val = []
    rows_form = []
    for row_data in data:
        row_v = [_make_cell(val) for val in row_data]
        row_f = [_make_cell(val) for val in row_data]
        rows_val.append(row_v)
        rows_form.append(row_f)

    rows_val_tuples = [tuple(row) for row in rows_val]
    rows_form_tuples = [tuple(row) for row in rows_form]

    ws_val = MagicMock()
    ws_val.title = name
    ws_val.sheet_state = state
    ws_val.iter_rows = MagicMock(side_effect=lambda: iter(rows_val_tuples))

    merge_mock = MagicMock()
    merge_mock.ranges = []
    ws_val.merged_cells = merge_mock
    ws_val._charts = []

    # cell() for merged cell resolution (unused but must not crash)
    def _cell_val(row, col):
        try:
            raw = rows_val[row - 1][col - 1]
            c = MagicMock()
            c.value = raw.value if hasattr(raw, "value") else raw
            return c
        except (IndexError, AttributeError):
            return MagicMock(value=None)

    ws_val.cell = _cell_val

    ws_form = MagicMock()
    ws_form.title = name
    ws_form.sheet_state = state
    ws_form.iter_rows = MagicMock(side_effect=lambda: iter(rows_form_tuples))
    ws_form.merged_cells = merge_mock

    def _cell_form(row, col):
        try:
            raw = rows_form[row - 1][col - 1]
            c = MagicMock()
            c.value = raw.value if hasattr(raw, "value") else raw
            return c
        except (IndexError, AttributeError):
            return MagicMock(value=None)

    ws_form.cell = _cell_form

    return ws_val, ws_form


def _make_workbook(sheets: list[tuple]):
    """
    Erstellt Mock-Workbooks.

    Args:
        sheets: Liste von (name, ws_values, ws_formulas) Tupeln.

    Returns:
        (wb_values, wb_formulas)

    WICHTIG: load_workbook wird in dieser Reihenfolge aufgerufen:
        1. data_only=False → wb_formulas
        2. data_only=True  → wb_values
    Side-effect muss [wb_form, wb_val] sein.
    """
    wb_val = MagicMock()
    wb_form = MagicMock()

    sheet_names = [s[0] for s in sheets]
    wb_val.sheetnames = sheet_names
    wb_form.sheetnames = sheet_names

    val_map = {s[0]: s[1] for s in sheets}
    form_map = {s[0]: s[2] for s in sheets}

    wb_val.__getitem__ = MagicMock(side_effect=lambda name: val_map[name])
    wb_form.__getitem__ = MagicMock(side_effect=lambda name: form_map[name])

    wb_val.close = MagicMock()
    wb_form.close = MagicMock()

    return wb_val, wb_form


def _side_effect(wb_val, wb_form):
    """load_workbook side_effect: first call = formulas (data_only=False), second = values."""
    return [wb_form, wb_val]


# =============================================================================
# Tests
# =============================================================================

class TestVisibleSheetsOnly:
    """test_visible_sheets_only: Keine Hidden Sheets → kein [HIDDEN] Tag."""

    def test_no_hidden_tag_in_output(self, tmp_path):
        """Nur sichtbare Sheets erzeugen keine [HIDDEN] Annotation."""
        ws1_v, ws1_f = _make_worksheet_with_state("Data", [["A", "B"], ["1", "2"]], "visible")
        ws2_v, ws2_f = _make_worksheet_with_state("Summary", [["Total"], ["42"]], "visible")
        wb_val, wb_form = _make_workbook([
            ("Data", ws1_v, ws1_f),
            ("Summary", ws2_v, ws2_f),
        ])

        with patch.object(_server, "OPENPYXL_AVAILABLE", True), \
             patch("openpyxl.load_workbook", side_effect=_side_effect(wb_val, wb_form)):
            result = convert_excel_enhanced(tmp_path / "test.xlsx")

        assert result["success"] is True
        assert "[HIDDEN]" not in result["markdown"]
        assert "[VERY HIDDEN]" not in result["markdown"]
        assert "## Sheet: Data" in result["markdown"]
        assert "## Sheet: Summary" in result["markdown"]

    def test_no_hidden_sheets_key_in_result(self, tmp_path):
        """Nur sichtbare Sheets → hidden_sheets nicht im Ergebnis-Dict."""
        ws_v, ws_f = _make_worksheet_with_state("Visible", [["X"], ["1"]], "visible")
        wb_val, wb_form = _make_workbook([("Visible", ws_v, ws_f)])

        with patch.object(_server, "OPENPYXL_AVAILABLE", True), \
             patch("openpyxl.load_workbook", side_effect=_side_effect(wb_val, wb_form)):
            result = convert_excel_enhanced(tmp_path / "test.xlsx")

        assert result["success"] is True
        assert "hidden_sheets" not in result


class TestHiddenSheetDetected:
    """test_hidden_sheet_detected: Hidden Sheet wird mit [HIDDEN] markiert."""

    def test_hidden_sheet_has_hidden_tag(self, tmp_path):
        """Ein hidden Sheet hat [HIDDEN] in der Überschrift."""
        ws_v, ws_f = _make_worksheet_with_state("SecretData", [["Col"], ["val"]], "hidden")
        wb_val, wb_form = _make_workbook([("SecretData", ws_v, ws_f)])

        with patch.object(_server, "OPENPYXL_AVAILABLE", True), \
             patch("openpyxl.load_workbook", side_effect=_side_effect(wb_val, wb_form)):
            result = convert_excel_enhanced(tmp_path / "test.xlsx")

        assert result["success"] is True
        assert "## Sheet: SecretData [HIDDEN]" in result["markdown"]
        assert "[VERY HIDDEN]" not in result["markdown"]

    def test_hidden_sheet_in_hidden_sheets_list(self, tmp_path):
        """Ein hidden Sheet erscheint in der hidden_sheets Liste."""
        ws_v, ws_f = _make_worksheet_with_state("HiddenSheet", [["X"], ["1"]], "hidden")
        wb_val, wb_form = _make_workbook([("HiddenSheet", ws_v, ws_f)])

        with patch.object(_server, "OPENPYXL_AVAILABLE", True), \
             patch("openpyxl.load_workbook", side_effect=_side_effect(wb_val, wb_form)):
            result = convert_excel_enhanced(tmp_path / "test.xlsx")

        assert result["success"] is True
        assert "hidden_sheets" in result
        assert "HiddenSheet" in result["hidden_sheets"]


class TestVeryHiddenSheetDetected:
    """test_very_hidden_sheet_detected: VeryHidden mit [VERY HIDDEN] markiert."""

    def test_very_hidden_sheet_has_very_hidden_tag(self, tmp_path):
        """Ein veryHidden Sheet hat [VERY HIDDEN] in der Überschrift."""
        ws_v, ws_f = _make_worksheet_with_state("InternalData", [["Col"], ["val"]], "veryHidden")
        wb_val, wb_form = _make_workbook([("InternalData", ws_v, ws_f)])

        with patch.object(_server, "OPENPYXL_AVAILABLE", True), \
             patch("openpyxl.load_workbook", side_effect=_side_effect(wb_val, wb_form)):
            result = convert_excel_enhanced(tmp_path / "test.xlsx")

        assert result["success"] is True
        assert "## Sheet: InternalData [VERY HIDDEN]" in result["markdown"]
        assert "[HIDDEN]" not in result["markdown"]

    def test_very_hidden_sheet_in_hidden_sheets_list(self, tmp_path):
        """Ein veryHidden Sheet erscheint in der hidden_sheets Liste."""
        ws_v, ws_f = _make_worksheet_with_state("VerySecret", [["X"], ["1"]], "veryHidden")
        wb_val, wb_form = _make_workbook([("VerySecret", ws_v, ws_f)])

        with patch.object(_server, "OPENPYXL_AVAILABLE", True), \
             patch("openpyxl.load_workbook", side_effect=_side_effect(wb_val, wb_form)):
            result = convert_excel_enhanced(tmp_path / "test.xlsx")

        assert result["success"] is True
        assert "hidden_sheets" in result
        assert "VerySecret" in result["hidden_sheets"]


class TestHiddenSheetsAfterVisible:
    """test_hidden_sheets_after_visible: Reihenfolge: visible zuerst, dann hidden."""

    def test_visible_before_hidden_in_markdown(self, tmp_path):
        """Sichtbare Sheets erscheinen vor Hidden Sheets im Markdown."""
        ws_vis_v, ws_vis_f = _make_worksheet_with_state("PublicData", [["A"], ["1"]], "visible")
        ws_hid_v, ws_hid_f = _make_worksheet_with_state("HiddenConfig", [["B"], ["2"]], "hidden")

        # Workbook order: hidden first, then visible — output should still be visible first
        wb_val, wb_form = _make_workbook([
            ("HiddenConfig", ws_hid_v, ws_hid_f),
            ("PublicData", ws_vis_v, ws_vis_f),
        ])

        with patch.object(_server, "OPENPYXL_AVAILABLE", True), \
             patch("openpyxl.load_workbook", side_effect=_side_effect(wb_val, wb_form)):
            result = convert_excel_enhanced(tmp_path / "test.xlsx")

        assert result["success"] is True
        md = result["markdown"]
        pos_visible = md.index("## Sheet: PublicData")
        pos_hidden = md.index("## Sheet: HiddenConfig [HIDDEN]")
        assert pos_visible < pos_hidden, "Visible sheet must appear before hidden sheet in output"

    def test_multiple_visible_before_multiple_hidden(self, tmp_path):
        """Mehrere sichtbare Sheets erscheinen alle vor den hidden Sheets."""
        ws_v1, ws_f1 = _make_worksheet_with_state("Vis1", [["A"], ["1"]], "visible")
        ws_v2, ws_f2 = _make_worksheet_with_state("Vis2", [["B"], ["2"]], "visible")
        ws_h1, ws_hf1 = _make_worksheet_with_state("Hid1", [["C"], ["3"]], "hidden")
        ws_h2, ws_hf2 = _make_worksheet_with_state("Hid2", [["D"], ["4"]], "veryHidden")

        wb_val, wb_form = _make_workbook([
            ("Vis1", ws_v1, ws_f1),
            ("Hid1", ws_h1, ws_hf1),
            ("Vis2", ws_v2, ws_f2),
            ("Hid2", ws_h2, ws_hf2),
        ])

        with patch.object(_server, "OPENPYXL_AVAILABLE", True), \
             patch("openpyxl.load_workbook", side_effect=_side_effect(wb_val, wb_form)):
            result = convert_excel_enhanced(tmp_path / "test.xlsx")

        assert result["success"] is True
        md = result["markdown"]

        pos_vis1 = md.index("## Sheet: Vis1")
        pos_vis2 = md.index("## Sheet: Vis2")
        pos_hid1 = md.index("## Sheet: Hid1 [HIDDEN]")
        pos_hid2 = md.index("## Sheet: Hid2 [VERY HIDDEN]")

        assert pos_vis1 < pos_hid1
        assert pos_vis1 < pos_hid2
        assert pos_vis2 < pos_hid1
        assert pos_vis2 < pos_hid2


class TestHiddenSheetsInMeta:
    """test_hidden_sheets_in_meta: meta.hidden_sheets wird gesetzt."""

    def test_hidden_sheets_propagated_via_convert_auto(self, tmp_path):
        """convert_auto setzt meta.hidden_sheets wenn Hidden Sheets vorhanden sind."""
        ws_vis_v, ws_vis_f = _make_worksheet_with_state("Public", [["A"], ["1"]], "visible")
        ws_hid_v, ws_hid_f = _make_worksheet_with_state("Private", [["B"], ["2"]], "hidden")

        wb_val, wb_form = _make_workbook([
            ("Public", ws_vis_v, ws_vis_f),
            ("Private", ws_hid_v, ws_hid_f),
        ])

        xlsx_data = b"PK fake xlsx data"

        with patch.object(_server, "OPENPYXL_AVAILABLE", True), \
             patch.object(_server, "detect_mimetype_from_bytes", return_value=None), \
             patch("openpyxl.load_workbook", side_effect=_side_effect(wb_val, wb_form)):
            response = run_async(convert_auto(
                file_data=xlsx_data,
                filename="report.xlsx",
                source="test",
                source_type="base64",
                input_meta={},
                show_formulas=False,
            ))

        assert response.success is True
        assert response.meta.hidden_sheets is not None
        assert "Private" in response.meta.hidden_sheets

    def test_very_hidden_sheet_in_meta(self, tmp_path):
        """meta.hidden_sheets enthält auch veryHidden Sheet-Namen."""
        ws_vis_v, ws_vis_f = _make_worksheet_with_state("Public", [["A"], ["1"]], "visible")
        ws_vh_v, ws_vh_f = _make_worksheet_with_state("VerySecret", [["B"], ["2"]], "veryHidden")

        wb_val, wb_form = _make_workbook([
            ("Public", ws_vis_v, ws_vis_f),
            ("VerySecret", ws_vh_v, ws_vh_f),
        ])

        xlsx_data = b"PK fake xlsx data"

        with patch.object(_server, "OPENPYXL_AVAILABLE", True), \
             patch.object(_server, "detect_mimetype_from_bytes", return_value=None), \
             patch("openpyxl.load_workbook", side_effect=_side_effect(wb_val, wb_form)):
            response = run_async(convert_auto(
                file_data=xlsx_data,
                filename="report.xlsx",
                source="test",
                source_type="base64",
                input_meta={},
                show_formulas=False,
            ))

        assert response.success is True
        assert response.meta.hidden_sheets is not None
        assert "VerySecret" in response.meta.hidden_sheets


class TestNoHiddenSheetsMetaNull:
    """test_no_hidden_sheets_meta_null: Kein Hidden → meta.hidden_sheets ist None."""

    def test_meta_hidden_sheets_is_none_when_no_hidden(self, tmp_path):
        """Wenn keine Hidden Sheets vorhanden sind, ist meta.hidden_sheets None."""
        ws_v, ws_f = _make_worksheet_with_state("OnlyVisible", [["A"], ["1"]], "visible")
        wb_val, wb_form = _make_workbook([("OnlyVisible", ws_v, ws_f)])

        xlsx_data = b"PK fake xlsx data"

        with patch.object(_server, "OPENPYXL_AVAILABLE", True), \
             patch.object(_server, "detect_mimetype_from_bytes", return_value=None), \
             patch("openpyxl.load_workbook", side_effect=_side_effect(wb_val, wb_form)):
            response = run_async(convert_auto(
                file_data=xlsx_data,
                filename="visible_only.xlsx",
                source="test",
                source_type="base64",
                input_meta={},
                show_formulas=False,
            ))

        assert response.success is True
        assert response.meta.hidden_sheets is None


class TestMixedVisibility:
    """test_mixed_visibility: Visible + Hidden + VeryHidden gemischt."""

    def test_all_three_states_correctly_labeled(self, tmp_path):
        """Alle drei Sichtbarkeits-Zustände werden korrekt annotiert."""
        ws_vis_v, ws_vis_f = _make_worksheet_with_state("Public", [["A"], ["1"]], "visible")
        ws_hid_v, ws_hid_f = _make_worksheet_with_state("Hidden", [["B"], ["2"]], "hidden")
        ws_vh_v, ws_vh_f = _make_worksheet_with_state("VeryHidden", [["C"], ["3"]], "veryHidden")

        wb_val, wb_form = _make_workbook([
            ("Public", ws_vis_v, ws_vis_f),
            ("Hidden", ws_hid_v, ws_hid_f),
            ("VeryHidden", ws_vh_v, ws_vh_f),
        ])

        with patch.object(_server, "OPENPYXL_AVAILABLE", True), \
             patch("openpyxl.load_workbook", side_effect=_side_effect(wb_val, wb_form)):
            result = convert_excel_enhanced(tmp_path / "test.xlsx")

        assert result["success"] is True
        md = result["markdown"]

        # Correct annotations
        assert "## Sheet: Public" in md
        assert "## Sheet: Public [" not in md  # no annotation on visible
        assert "## Sheet: Hidden [HIDDEN]" in md
        assert "## Sheet: VeryHidden [VERY HIDDEN]" in md

        # hidden_sheets list contains both non-visible sheets
        assert "hidden_sheets" in result
        assert "Hidden" in result["hidden_sheets"]
        assert "VeryHidden" in result["hidden_sheets"]
        assert "Public" not in result["hidden_sheets"]

    def test_mixed_meta_via_convert_auto(self, tmp_path):
        """convert_auto gibt korrekte hidden_sheets in meta zurück (gemischte Sichtbarkeit)."""
        ws_vis_v, ws_vis_f = _make_worksheet_with_state("Open", [["A"], ["1"]], "visible")
        ws_hid_v, ws_hid_f = _make_worksheet_with_state("Closed", [["B"], ["2"]], "hidden")
        ws_vh_v, ws_vh_f = _make_worksheet_with_state("Buried", [["C"], ["3"]], "veryHidden")

        wb_val, wb_form = _make_workbook([
            ("Open", ws_vis_v, ws_vis_f),
            ("Closed", ws_hid_v, ws_hid_f),
            ("Buried", ws_vh_v, ws_vh_f),
        ])

        xlsx_data = b"PK fake xlsx data"

        with patch.object(_server, "OPENPYXL_AVAILABLE", True), \
             patch.object(_server, "detect_mimetype_from_bytes", return_value=None), \
             patch("openpyxl.load_workbook", side_effect=_side_effect(wb_val, wb_form)):
            response = run_async(convert_auto(
                file_data=xlsx_data,
                filename="mixed.xlsx",
                source="test",
                source_type="base64",
                input_meta={},
                show_formulas=False,
            ))

        assert response.success is True
        assert response.meta.hidden_sheets is not None
        assert "Closed" in response.meta.hidden_sheets
        assert "Buried" in response.meta.hidden_sheets
        assert "Open" not in response.meta.hidden_sheets
