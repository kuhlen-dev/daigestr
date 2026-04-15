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
    assert "warnings" in contract["success_response_fields"]
    assert "normalized" in contract["success_response_fields"]
    assert "error" in contract["required_top_level_fields"]
    assert "execution_id" in contract["required_meta_fields"]
    assert "contract_version" in contract["required_meta_fields"]
    assert contract["null_semantics"]["null"]
    assert contract["error_semantics"]["success_false"]
    assert contract["job_progress_endpoints"]["status"] == "GET /v1/jobs/{id} returns canonical progress under progress."
    assert "execution_id" in contract["job_progress_endpoints"]["start"]


def test_tips_document_job_progress_fields(monkeypatch):
    monkeypatch.setattr(_routing, "get_all_template_ids", lambda: ["invoice"])

    result = _server._build_tips_dict()

    progress_fields = result["response_contract"]["job_progress_fields"]
    assert "progress.current_stage" in progress_fields
    assert "progress.page_current" in progress_fields
    assert "progress.request_id" in progress_fields


def test_tips_document_classify_policy_default(monkeypatch):
    monkeypatch.setattr(_routing, "get_all_template_ids", lambda: ["invoice"])
    monkeypatch.setattr(_routing, "DEFAULT_CLASSIFY", True)

    result = _server._build_tips_dict()

    classify_feature = result["optional_features"]["classify"]
    assert classify_feature["default"] is True
    assert "DEFAULT_CLASSIFY" in classify_feature["description"]


def test_tips_document_execution_status_fields(monkeypatch):
    monkeypatch.setattr(_routing, "get_all_template_ids", lambda: ["invoice"])

    result = _server._build_tips_dict()

    execution_fields = result["response_contract"]["execution_status_fields"]
    assert "execution_id" in execution_fields
    assert "idempotency_key" in execution_fields
    assert "input_snapshot" in execution_fields
    assert "status" in execution_fields
    assert "progress" in execution_fields
    assert "result_meta_summary" in execution_fields
    assert "attempts" in execution_fields
    assert "subjobs" in execution_fields
    assert result["response_contract"]["execution_endpoints"]["status"] == "GET /v1/executions/{id} returns the canonical execution status including attempts, upstream subjobs, and canonical progress."
    assert result["response_contract"]["idempotency"]["request_meta_key"] == "meta.idempotency_key optionally pins repeated requests to the same canonical execution instead of creating a duplicate logical run."
    assert result["response_contract"]["diagnostics_endpoints"]["executions"] == "GET /v1/diagnostics/executions returns active executions, stuck executions, and normalization drift diagnostics for operators."
    diagnostics_fields = result["response_contract"]["diagnostics_fields"]
    assert "active_count" in diagnostics_fields
    assert "stuck_executions" in diagnostics_fields
    assert "normalizer_drift" in diagnostics_fields


def test_tips_document_policy_resolution(monkeypatch):
    monkeypatch.setattr(_routing, "get_all_template_ids", lambda: ["invoice"])
    monkeypatch.setattr(_server, "DEFAULT_CLASSIFY", True)
    monkeypatch.setattr(_server, "ALLOWED_PATH_ROOTS", ["/data", "/shared"])
    monkeypatch.setattr(_server, "ALLOW_SYMLINK_PATHS", False)
    monkeypatch.setattr(_server, "QUEUE_ENABLED", True)
    monkeypatch.setattr(_server, "QUEUE_WORKER_COUNT", 2)
    monkeypatch.setattr(_server, "QUEUE_POLL_INTERVAL_SECONDS", 1.0)
    monkeypatch.setattr(_server, "QUEUE_LEASE_SECONDS", 300)
    monkeypatch.setattr(_server, "QUALITY_RETRY_ENABLED", True)
    monkeypatch.setattr(_server, "QUALITY_RETRY_THRESHOLD", 0.75)
    monkeypatch.setattr(_server, "QUALITY_RETRY_MODE", "full")
    monkeypatch.setattr(_server, "LONG_DOCUMENT_PAGE_THRESHOLD", 25)
    monkeypatch.setattr(_server, "PAGE_DESCRIBE_MAX_PAGES", 50)
    monkeypatch.setattr(_server, "CONTRACT_VERSION", "1.0")

    result = _server._build_tips_dict()

    policy = result["policy_resolution"]
    assert policy["classify_policy"]["default_classify"] is True
    assert policy["retry_policy"]["enabled"] is True
    assert policy["retry_policy"]["threshold"] == 0.75
    assert policy["long_document_policy"]["page_threshold"] == 25
    assert policy["normalization_policy"]["resolved_template_order"][0] == "meta.template_used"
    assert policy["storage_policy"]["allowed_path_roots"] == ["/data", "/shared"]
    assert policy["storage_policy"]["allow_symlink_paths"] is False
    assert policy["storage_policy"]["violation_error_code"] == "PATH_NOT_ALLOWED"
    assert policy["async_queue_policy"]["enabled"] is True
    assert policy["async_queue_policy"]["worker_count"] == 2
    assert policy["async_queue_policy"]["lease_seconds"] == 300
    assert "warnings" in result["response_fields"]
    assert "meta.contract_version" in result["canonical_meta_fields"]


def test_tips_document_brix_integration_contract(monkeypatch):
    monkeypatch.setattr(_routing, "get_all_template_ids", lambda: ["invoice"])

    result = _server._build_tips_dict()

    contract = result["brix_integration_contract"]
    assert "meta.template_used" in contract["read_from_raw_meta"]
    assert "meta.quality_score" in contract["read_from_raw_meta"]
    assert "normalized._quality_score as document quality" in contract["do_not_use_as_canonical"]


def test_tips_document_batch_creation_contract(monkeypatch):
    monkeypatch.setattr(_routing, "get_all_template_ids", lambda: ["invoice"])
    monkeypatch.setattr(_server, "BATCH_DEFAULT_QUEUE_NAME", "default")

    result = _server._build_tips_dict()

    batch_policy = result["policy_resolution"]["batch_queue_policy"]
    assert batch_policy["default_queue_name"] == "default"
    assert "POST /v1/batches" in batch_policy["create_endpoint"]

    batch_endpoints = result["response_contract"]["batch_endpoints"]
    assert "POST /v1/batches" in batch_endpoints["start"]
    assert "GET /v1/batches/{id}" in batch_endpoints["status"]
    assert "GET /v1/batches" in batch_endpoints["list"]
    assert "GET /v1/batches/{id}/items" in batch_endpoints["items"]
    assert "GET /v1/batches/{id}/items/{item_id}/result" in batch_endpoints["item_result"]
    assert "execution_kind=batch_item" in batch_endpoints["linkage"]


def test_tips_document_mistral_batch_policy(monkeypatch):
    monkeypatch.setattr(_routing, "get_all_template_ids", lambda: ["invoice"])
    monkeypatch.setattr(_server, "MISTRAL_BATCH_ENABLED", True)
    monkeypatch.setattr(_server, "MISTRAL_BATCH_MIN_ITEMS", 10)
    monkeypatch.setattr(_server, "MISTRAL_BATCH_POLL_INTERVAL_SECONDS", 5)
    monkeypatch.setattr(_server, "MISTRAL_BATCH_MAX_ACTIVE", 25)
    monkeypatch.setattr(_server, "MISTRAL_BATCH_TIMEOUT_HOURS", 24)
    monkeypatch.setattr(_server, "MISTRAL_BATCH_ALLOWED_SOURCE_TYPES", ("file", "base64"))

    result = _server._build_tips_dict()

    mistral_batch_policy = result["policy_resolution"]["batch_queue_policy"]["mistral_batch_policy"]
    assert mistral_batch_policy["enabled"] is True
    assert mistral_batch_policy["min_items"] == 10
    assert mistral_batch_policy["poll_interval_seconds"] == 5
    assert mistral_batch_policy["max_active"] == 25
    assert mistral_batch_policy["timeout_hours"] == 24
    assert mistral_batch_policy["allowed_source_types"] == ["file", "base64"]
    assert "preferred_dispatch_target" in mistral_batch_policy["decision_note"]
    assert "MISTRAL_BATCH_ENABLED" in mistral_batch_policy["decision_note"]


def test_tips_document_retention_policy(monkeypatch):
    monkeypatch.setattr(_routing, "get_all_template_ids", lambda: ["invoice"])
    monkeypatch.setattr(_server, "EXECUTION_RESULT_RETENTION_DAYS", 30)
    monkeypatch.setattr(_server, "EXECUTION_RESULT_ARTIFACT_RETENTION_DAYS", 14)
    monkeypatch.setattr(_server, "DEBUG_SNAPSHOTS_RETENTION_DAYS", 5)

    result = _server._build_tips_dict()

    retention = result["policy_resolution"]["retention_policy"]
    assert retention["execution_metadata"]["retention_days"] is None
    assert retention["result_payload"]["retention_days"] == 30
    assert retention["debug_snapshot"]["retention_days"] == 5
    assert retention["replay_artifact"]["retention_days"] == 14
    assert "execution_result_cleanup" in retention["result_payload"]["cleanup_behavior"]
