"""
Tests for E10/W10.4/T10.4.4 tips contract alignment.
"""

from conftest import load_server_module


_server = load_server_module(use_real_pil=False)
import routing as _routing


def test_tips_document_canonical_meta_fields(monkeypatch):
    monkeypatch.setattr(_routing, "get_all_template_ids", lambda: ["invoice", "receipt"])

    result = _server._build_tips_dict()

    assert result["canonical_meta_fields"]["meta.template_used"]
    assert result["canonical_meta_fields"]["meta.quality_score"]
    assert result["canonical_meta_fields"]["meta.retry_applied"]


def test_tips_do_not_document_normalize_request_parameter(monkeypatch):
    monkeypatch.setattr(_routing, "get_all_template_ids", lambda: ["invoice"])

    result = _server._build_tips_dict()

    assert "normalize" not in result["normalization"]["parameters"]
    assert "compact" in result["normalization"]["parameters"]


def test_tips_explain_quality_score_layers(monkeypatch):
    monkeypatch.setattr(_routing, "get_all_template_ids", lambda: ["invoice"])

    result = _server._build_tips_dict()

    score_semantics = result["normalization"]["score_semantics"]
    assert "meta.quality_score" in score_semantics
    assert "normalized._quality_score" in score_semantics
