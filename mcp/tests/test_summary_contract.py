"""
Tests for E10/W10.4/T10.4.2 summary field harmonization.
"""

from conftest import load_server_module


_server = load_server_module(use_real_pil=False)
import intelligence as _intelligence


def test_harmonize_summary_prefers_top_level_summary():
    extracted = {
        "summary": "Full document summary",
        "zusammenfassung": "Legacy summary",
        "_meta": {"zusammenfassung": "Meta summary"},
    }

    result = _intelligence._harmonize_extracted_summary_fields(extracted)

    assert result["summary"] == "Full document summary"
    assert result["zusammenfassung"] == "Full document summary"
    assert result["_meta"]["zusammenfassung"] == "Full document summary"


def test_harmonize_summary_falls_back_to_meta_summary():
    extracted = {
        "_meta": {"zusammenfassung": "Meta summary"},
    }

    result = _intelligence._harmonize_extracted_summary_fields(extracted)

    assert result["summary"] == "Meta summary"
    assert result["_meta"]["zusammenfassung"] == "Meta summary"


def test_harmonize_summary_does_not_invent_empty_values():
    extracted = {"other": "value", "_meta": {}}

    result = _intelligence._harmonize_extracted_summary_fields(extracted)

    assert "summary" not in result
    assert result["_meta"] == {}
