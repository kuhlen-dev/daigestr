"""
Tests for E10/W10.5/T10.5.1 API documentation alignment.
"""

import sys
from unittest.mock import MagicMock

from conftest import load_server_module


class _PassthroughMCP:
    def tool(self, name=None, **kwargs):
        def decorator(f):
            return f
        return decorator

    def run(self, *args, **kwargs):
        pass


def _load_server_with_passthrough_mcp():
    fastmcp_mock = MagicMock()
    fastmcp_mock.FastMCP = MagicMock(return_value=_PassthroughMCP())
    sys.modules["fastmcp"] = fastmcp_mock

    if "server" in sys.modules:
        del sys.modules["server"]

    return load_server_module(use_real_pil=False)


_server = _load_server_with_passthrough_mcp()


def _load_server_with_route_capture():
    fastmcp_mock = MagicMock()
    fastmcp_mock.FastMCP = MagicMock(return_value=_PassthroughMCP())
    sys.modules["fastmcp"] = fastmcp_mock

    registered: list[tuple[str, str]] = []
    fastapi_mock = MagicMock()
    app_mock = MagicMock()

    def _capture(method_name):
        def decorator(path, **_kwargs):
            def _wrap(func):
                registered.append((method_name, path))
                return func
            return _wrap
        return decorator

    app_mock.post = _capture("post")
    app_mock.get = _capture("get")
    app_mock.delete = _capture("delete")
    app_mock.put = _capture("put")
    app_mock.exception_handler = lambda *a, **kw: (lambda f: f)
    app_mock.include_router = MagicMock()
    fastapi_mock.FastAPI = MagicMock(return_value=app_mock)
    fastapi_mock.HTTPException = Exception
    fastapi_mock.Request = MagicMock()

    if "server" in sys.modules:
        del sys.modules["server"]

    server = load_server_module(
        use_real_pil=False,
        extra_patches={
            "fastapi": fastapi_mock,
            "fastapi.exceptions": MagicMock(),
            "fastapi.responses": MagicMock(),
        },
    )
    server._registered_routes = registered  # type: ignore[attr-defined]
    return server


_server_openapi = _load_server_with_route_capture()


def test_convert_request_extract_schema_description_mentions_alternatives():
    from models import ConvertRequest

    description = ConvertRequest.model_fields["extract_schema"].description
    assert "template" in description
    assert "auto_extract=true" in description
    assert "always null" not in description


def test_mcp_convert_docstring_mentions_canonical_meta_fields():
    doc = _server.mcp_convert.__doc__ or ""

    assert "meta.document_type" in doc
    assert "meta.template_used" in doc
    assert "meta.quality_score" in doc
    assert "output_format" in doc


def test_mcp_extract_docstring_mentions_normalized_score_layer():
    doc = _server.mcp_extract.__doc__ or ""

    assert "normalized._quality_score" in doc
    assert "meta.retry_applied" in doc


def test_request_template_descriptions_do_not_hardcode_template_names():
    from models import ConvertRequest, ExtractRequest

    convert_description = ConvertRequest.model_fields["template"].description
    extract_description = ExtractRequest.model_fields["template"].description

    assert "live template registry" in convert_description
    assert "invoice', 'cv', 'contract" not in convert_description
    assert "live template registry" in extract_description
    assert "invoice', 'cv', 'contract" not in extract_description


def test_openapi_documents_execution_batch_and_replay_paths():
    paths = {path for _method, path in _server_openapi._registered_routes}

    assert "/v1/jobs/{job_id}" in paths
    assert "/v1/jobs/{job_id}/result" in paths
    assert "/v1/executions/{execution_id}" in paths
    assert "/v1/executions/{execution_id}/result" in paths
    assert "/v1/executions/{execution_id}/replay" in paths
    assert "/v1/batches/{batch_id}" in paths
    assert "/v1/batches/{batch_id}/items" in paths
    assert "/v1/batches/{batch_id}/items/{batch_item_id}/result" in paths
    assert "/v1/batches/{batch_id}/items/{batch_item_id}/replay" in paths
    assert "/v1/diagnostics/executions" in paths


def test_openapi_execution_and_batch_schemas_include_new_contract_fields():
    from models import BatchItemResponse, BatchStatusResponse, ExecutionDiagnosticsResponse, ExecutionStatusResponse

    execution_status = ExecutionStatusResponse.model_json_schema()["properties"]
    batch_status = BatchStatusResponse.model_json_schema()["properties"]
    batch_item = BatchItemResponse.model_json_schema()["properties"]
    diagnostics = ExecutionDiagnosticsResponse.model_json_schema()["properties"]
    assert "result_artifact_refs" in execution_status
    assert "subjobs" in execution_status
    assert "final_result_available" in execution_status

    batch_status = BatchStatusResponse.model_json_schema()["properties"]
    assert "cancelled_count" in batch_status
    assert "active_items" in batch_status

    batch_item = BatchItemResponse.model_json_schema()["properties"]
    assert "result_artifact_refs" in batch_item
    assert "input_snapshot" in batch_item

    assert "normalizer_drift" in diagnostics


def test_openapi_examples_cover_async_execution_replay_and_health_shapes():
    from models import HealthResponse, JobStatusResponse, ReplayStartResponse, ExecutionStatusResponse

    assert JobStatusResponse.model_config["json_schema_extra"]["example"]["progress"]["current_stage"] == "extract"
    assert ExecutionStatusResponse.model_config["json_schema_extra"]["example"]["execution_kind"] == "async"
    assert ReplayStartResponse.model_config["json_schema_extra"]["example"]["execution_kind"] == "replay"
    assert "/v1/executions/exec-replay-123/result" in ReplayStartResponse.model_config["json_schema_extra"]["example"]["result_path"]
    assert HealthResponse.model_config["json_schema_extra"]["example"]["meta"]["operator_policy"]["replay_api_enabled"] is False
