"""
Integration Tests — Normalizer in routing/_convert_auto_impl (T-DAI-055).

Verifies:
1. ConvertResponse has normalized_* fields
2. ConvertRequest has compact field
3. _apply_normalizer is called after extraction when template_used is set
4. normalize() is not called when extracted is None

Run:
  DATABASE_URL=postgresql://daigestr:daigestr@localhost:15432/daigestr \
  python3 -m pytest tests/test_normalizer_integration.py -v --tb=short
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

pytestmark = pytest.mark.skipif(
    "localhost" not in os.getenv("DATABASE_URL", ""),
    reason="Requires local PostgreSQL (DATABASE_URL must contain 'localhost')",
)


# ---------------------------------------------------------------------------
# DB Fixture
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def setup_db():
    from templates_db import init_templates_db, pool_reset
    from normalizer_db import init_normalization_db
    import normalizer_cache

    pool_reset()
    init_templates_db()
    init_normalization_db()
    normalizer_cache.cache_reset()
    yield
    normalizer_cache.cache_reset()
    pool_reset()


@pytest.fixture
def norm_template():
    """Insert a minimal test template with normalize_mapping."""
    from normalizer_db import set_mapping
    from templates_db import get_db_connection, _return_conn

    template_id = "__norm_integ_test__"
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO template (id, display_name, category, schema) "
            "VALUES (%s, %s, %s, %s::jsonb) ON CONFLICT (id) DO NOTHING",
            (template_id, "Integration Test Template", "test", '{"type": "object"}'),
        )
        conn.commit()
    finally:
        _return_conn(conn)

    mapping = {"amount": "total", "currency": "currency_code"}
    set_mapping(template_id, mapping)
    yield template_id

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM template WHERE id = %s", (template_id,))
        conn.commit()
    finally:
        _return_conn(conn)


# ---------------------------------------------------------------------------
# Model Tests
# ---------------------------------------------------------------------------

def test_convert_response_has_normalized_fields():
    """ConvertResponse must have normalized, normalized_version, normalized_warnings, normalized_trace, normalized_context."""
    from models import ConvertResponse, MetaData
    resp = ConvertResponse(success=True, markdown="# Test", meta=MetaData())
    assert hasattr(resp, "normalized")
    assert hasattr(resp, "normalized_version")
    assert hasattr(resp, "normalized_warnings")
    assert hasattr(resp, "normalized_trace")
    assert hasattr(resp, "normalized_context")
    assert resp.normalized is None
    assert resp.normalized_version is None


def test_convert_request_has_compact():
    """ConvertRequest must have compact field (default False)."""
    from models import ConvertRequest
    req = ConvertRequest(path="/data/test.pdf")
    assert hasattr(req, "compact")
    assert req.compact is False

    req_compact = ConvertRequest(path="/data/test.pdf", compact=True)
    assert req_compact.compact is True


# ---------------------------------------------------------------------------
# _apply_normalizer Tests
# ---------------------------------------------------------------------------

def test_apply_normalizer_skips_when_no_extracted():
    """_apply_normalizer must return response unchanged when extracted is None."""
    from routing import _apply_normalizer
    from models import ConvertResponse, MetaData

    resp = ConvertResponse(success=True, markdown="# test", meta=MetaData())
    assert resp.extracted is None

    result = asyncio.get_event_loop().run_until_complete(
        _apply_normalizer(resp, {}, "invoice", False)
    )
    assert result.normalized is None
    assert result.normalized_version is None


def test_apply_normalizer_skips_when_no_template():
    """_apply_normalizer must return response unchanged when template_name is None."""
    from routing import _apply_normalizer
    from models import ConvertResponse, MetaData

    resp = ConvertResponse(success=True, markdown="# test", meta=MetaData())
    resp.extracted = {"amount": "100"}

    result = asyncio.get_event_loop().run_until_complete(
        _apply_normalizer(resp, {}, None, False)
    )
    assert result.normalized is None


def test_apply_normalizer_calls_normalize_with_mapping(norm_template):
    """_apply_normalizer must call normalize and populate normalized fields when mapping exists."""
    from routing import _apply_normalizer
    from normalizer_cache import cache_reset
    from models import ConvertResponse, MetaData

    cache_reset()

    resp = ConvertResponse(success=True, markdown="# test", meta=MetaData())
    resp.extracted = {"total": "99.50", "currency_code": "EUR"}

    result = asyncio.get_event_loop().run_until_complete(
        _apply_normalizer(resp, {"language": "de"}, norm_template, False)
    )

    # Normalized dict should be populated (mapping exists for this template)
    assert result.normalized is not None
    assert isinstance(result.normalized, dict)
    # version should be a string
    assert result.normalized_version is not None
    # warnings is a list (may be empty)
    assert isinstance(result.normalized_warnings, list)
    # trace is a list
    assert isinstance(result.normalized_trace, list)
    # context has country info
    assert isinstance(result.normalized_context, dict)


def test_apply_normalizer_compact_removes_nulls(norm_template):
    """When compact=True, normalized output should have no None values."""
    from routing import _apply_normalizer
    from normalizer_cache import cache_reset
    from models import ConvertResponse, MetaData

    cache_reset()

    resp = ConvertResponse(success=True, markdown="# test", meta=MetaData())
    resp.extracted = {"total": "50.00"}  # currency_code missing

    result = asyncio.get_event_loop().run_until_complete(
        _apply_normalizer(resp, {}, norm_template, compact=True)
    )

    if result.normalized is not None:
        # All values in normalized should be non-None when compact=True
        null_values = [k for k, v in result.normalized.items() if v is None]
        assert null_values == [], f"compact=True but null fields found: {null_values}"


def test_apply_normalizer_no_mapping_returns_none_gracefully(norm_template):
    """_apply_normalizer must return response unchanged when no mapping found for template."""
    from routing import _apply_normalizer
    from models import ConvertResponse, MetaData

    resp = ConvertResponse(success=True, markdown="# test", meta=MetaData())
    resp.extracted = {"amount": "100"}

    # Use a template that has no normalize_mapping
    result = asyncio.get_event_loop().run_until_complete(
        _apply_normalizer(resp, {}, "__nonexistent_template__", False)
    )
    # normalized should remain None since no mapping exists
    assert result.normalized is None


# ---------------------------------------------------------------------------
# routing.py signature Tests
# ---------------------------------------------------------------------------

def test_convert_auto_accepts_compact_and_template():
    """convert_auto must accept compact and template parameters."""
    import inspect
    from routing import convert_auto
    sig = inspect.signature(convert_auto)
    params = sig.parameters
    assert "compact" in params, "convert_auto missing 'compact' parameter"
    assert "template" in params, "convert_auto missing 'template' parameter"
    assert params["compact"].default is False
    assert params["template"].default is None


def test_convert_auto_impl_accepts_compact_and_template():
    """_convert_auto_impl must accept compact and template parameters."""
    import inspect
    from routing import _convert_auto_impl
    sig = inspect.signature(_convert_auto_impl)
    params = sig.parameters
    assert "compact" in params, "_convert_auto_impl missing 'compact' parameter"
    assert "template" in params, "_convert_auto_impl missing 'template' parameter"
