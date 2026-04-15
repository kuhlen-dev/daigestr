from pathlib import Path

from conftest import load_server_module


_server = load_server_module(use_real_pil=False)
import routing as _routing
import api_rest as _api_rest
import api_rest_audit as _audit_api
from models import BatchItemResponse, BatchStatusResponse, ExecutionStatusResponse


def test_wave_16_5_batch_contract_ring(monkeypatch):
    monkeypatch.setattr(_routing, "get_all_template_ids", lambda: ["invoice"])
    tips = _server._build_tips_dict()
    batch_endpoints = tips["response_contract"]["batch_endpoints"]

    assert "cancel" in batch_endpoints
    assert "resume" in batch_endpoints
    assert "item_retry" in batch_endpoints
    assert "result_artifact_refs" in batch_endpoints["item_history_shape"]
    assert "cancelled_count" in BatchStatusResponse.model_fields
    assert "final_result_available" in BatchItemResponse.model_fields


def test_wave_16_7_replay_and_retention_ring(monkeypatch):
    monkeypatch.setattr(_routing, "get_all_template_ids", lambda: ["invoice"])
    tips = _server._build_tips_dict()

    assert "replay_endpoints" in tips["response_contract"]
    assert tips["policy_resolution"]["retention_policy"]["result_payload"]["retention_days"] == _server.EXECUTION_RESULT_RETENTION_DAYS
    assert "result_artifact_refs" in ExecutionStatusResponse.model_fields


def test_wave_16_8_governance_ring(monkeypatch):
    monkeypatch.setattr(_api_rest, "check_persistence_health", lambda: {"ready": True, "database_url_configured": True, "connection_ok": True, "missing_tables": []})
    monkeypatch.setattr(_api_rest, "get_normalization_drift_summary", lambda limit=10: {"coverage_pct": 100.0, "templates_without_mapping": []})
    health_example = _server.HealthResponse.model_config["json_schema_extra"]["example"]

    assert "operator_policy" in health_example["meta"]
    assert "replay_api_enabled" in health_example["meta"]["operator_policy"]
    assert _server.PII_STORAGE_MODE in {"strict", "standard"}


def test_wave_16_9_documentation_ring():
    repo_root = Path(__file__).resolve().parents[2]

    assert (repo_root / "README.md").exists()
    assert (repo_root / "OPERATIONS.md").exists()
    assert Path(__file__).resolve().parent.joinpath("test_docs_sync_contract.py").exists()


def test_wave_16_10_consistency_ring(monkeypatch):
    events = [
        {
            "id": 1,
            "request_id": "req-1",
            "execution_id": "exec-1",
            "job_id": "job-1",
            "event_type": "response",
            "level": "warning",
            "created_at": "2026-04-15T10:00:05Z",
            "metadata": {
                "template_used": "invoice",
                "quality_score": 0.91,
                "warning_codes": ["used_retry"],
            },
        }
    ]
    summary = _audit_api._build_history_summary(events)

    assert summary["execution_id"] == "exec-1"
    assert summary["template_used"] == "invoice"
    assert summary["warning_codes"] == ["used_retry"]
