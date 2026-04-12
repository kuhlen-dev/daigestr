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
