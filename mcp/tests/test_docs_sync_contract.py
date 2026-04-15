from pathlib import Path

from conftest import load_server_module


_server = load_server_module(use_real_pil=False)
import routing as _routing
_repo_root = Path(__file__).resolve().parents[2]


def _read_doc(name: str) -> str:
    return (_repo_root / name).read_text(encoding="utf-8")


def test_readme_and_operations_document_canonical_execution_surfaces(monkeypatch):
    monkeypatch.setattr(_routing, "get_all_template_ids", lambda: ["invoice"])

    readme = _read_doc("README.md")
    operations = _read_doc("OPERATIONS.md")
    tips = _server._build_tips_dict()

    assert "/v1/executions/{id}" in readme
    assert "/v1/batches/{id}/items/{item_id}/result" in readme
    assert "result_artifact_refs" in readme
    assert "GET /v1/executions/{execution_id}" in operations
    assert "GET /v1/batches/{batch_id}/items" in operations
    assert tips["response_contract"]["execution_endpoints"]["status"].startswith("GET /v1/executions/{id}")
    assert "GET /v1/batches/{id}/items/{item_id}/result" in tips["response_contract"]["batch_endpoints"]["item_result"]


def test_docs_and_tips_document_operator_boundaries(monkeypatch):
    monkeypatch.setattr(_routing, "get_all_template_ids", lambda: ["invoice"])
    monkeypatch.setattr(_server, "AUDIT_API_ENABLED", True)
    monkeypatch.setattr(_server, "DEBUG_SNAPSHOT_API_ENABLED", False)
    monkeypatch.setattr(_server, "REPLAY_API_ENABLED", False)

    readme = _read_doc("README.md")
    operations = _read_doc("OPERATIONS.md")
    tips = _server._build_tips_dict()

    assert "REPLAY_API_ENABLED" in readme
    assert "operator_action" in readme
    assert "REPLAY_API_ENABLED" in operations
    assert "operator_action" in operations
    assert tips["policy_resolution"]["operator_boundary_policy"]["replay_api_enabled"] is False
    assert "operator_action" in tips["policy_resolution"]["operator_boundary_policy"]["auditability"]["operator_actions"]


def test_docs_and_tips_document_polling_modes(monkeypatch):
    monkeypatch.setattr(_routing, "get_all_template_ids", lambda: ["invoice"])

    readme = _read_doc("README.md")
    operations = _read_doc("OPERATIONS.md")
    tips = _server._build_tips_dict()
    polling = tips["response_contract"]["polling_vs_result_retrieval"]

    assert "POST /v1/convert/async" in readme
    assert "POST /v1/batches" in readme
    assert "Replay endpoints return a new replay execution" in polling["replay"]
    assert "compatibility surfaces for async callers" in operations
    assert "poll /v1/jobs/{id} or /v1/executions/{execution_id}" in polling["async"]
