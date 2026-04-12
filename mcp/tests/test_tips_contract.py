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


def test_tips_document_response_contract_and_null_semantics(monkeypatch):
    monkeypatch.setattr(_routing, "get_all_template_ids", lambda: ["invoice"])

    result = _server._build_tips_dict()

    contract = result["response_contract"]
    assert "markdown" in contract["success_response_fields"]
    assert "normalized" in contract["success_response_fields"]
    assert contract["null_semantics"]["null"]
    assert contract["job_progress_endpoints"]["status"] == "GET /v1/jobs/{id} returns canonical progress under progress."


def test_tips_document_job_progress_fields(monkeypatch):
    monkeypatch.setattr(_routing, "get_all_template_ids", lambda: ["invoice"])

    result = _server._build_tips_dict()

    progress_fields = result["response_contract"]["job_progress_fields"]
    assert "progress.current_stage" in progress_fields
    assert "progress.page_current" in progress_fields
    assert "progress.request_id" in progress_fields


def test_tips_document_brix_integration_contract(monkeypatch):
    monkeypatch.setattr(_routing, "get_all_template_ids", lambda: ["invoice"])

    result = _server._build_tips_dict()

    contract = result["brix_integration_contract"]
    assert "meta.template_used" in contract["read_from_raw_meta"]
    assert "meta.quality_score" in contract["read_from_raw_meta"]
    assert "normalized._quality_score as document quality" in contract["do_not_use_as_canonical"]
