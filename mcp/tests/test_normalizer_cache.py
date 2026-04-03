"""
Tests für normalizer_cache.py (T-DAI-054).

Run: DATABASE_URL=postgresql://daigestr:daigestr@localhost:15432/daigestr
     python3 -m pytest tests/test_normalizer_cache.py -v --tb=short
"""

import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

pytestmark = pytest.mark.skipif(
    "localhost" not in os.getenv("DATABASE_URL", ""),
    reason="Requires local PostgreSQL (DATABASE_URL must contain 'localhost')",
)


@pytest.fixture(autouse=True)
def reset_cache_and_db():
    """Reset cache + DB pool before each test."""
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


# ---------------------------------------------------------------------------
# cache_reset
# ---------------------------------------------------------------------------

def test_cache_reset_clears_state():
    import normalizer_cache
    # Populate cache
    normalizer_cache.get_cached_fields()
    assert normalizer_cache._cache["fields"] is not None
    # Reset
    normalizer_cache.cache_reset()
    assert normalizer_cache._cache["fields"] is None
    assert normalizer_cache._cache["values"] == {}
    assert normalizer_cache._cache["mappings"] == {}
    assert normalizer_cache._cache["categories"] is None
    assert normalizer_cache._meta["version_hash"] is None


# ---------------------------------------------------------------------------
# get_cached_fields
# ---------------------------------------------------------------------------

def test_get_cached_fields_returns_list():
    from normalizer_cache import get_cached_fields
    fields = get_cached_fields()
    assert isinstance(fields, list)


def test_get_cached_fields_cached_on_second_call():
    import normalizer_cache
    first = normalizer_cache.get_cached_fields()
    # Second call must return same object (from cache)
    second = normalizer_cache.get_cached_fields()
    assert first is second


def test_get_cached_fields_populates_version_hash():
    import normalizer_cache
    assert normalizer_cache._meta["version_hash"] is None
    normalizer_cache.get_cached_fields()
    assert normalizer_cache._meta["version_hash"] is not None


# ---------------------------------------------------------------------------
# get_cached_values
# ---------------------------------------------------------------------------

def test_get_cached_values_returns_list():
    from normalizer_cache import get_cached_values
    vals = get_cached_values("currency")
    assert isinstance(vals, list)


def test_get_cached_values_cached():
    import normalizer_cache
    normalizer_cache.get_cached_values("currency")
    assert "currency" in normalizer_cache._cache["values"]
    first = normalizer_cache._cache["values"]["currency"]
    normalizer_cache.get_cached_values("currency")
    assert normalizer_cache._cache["values"]["currency"] is first


def test_get_cached_values_different_fields_cached_separately():
    import normalizer_cache
    normalizer_cache.get_cached_values("currency")
    normalizer_cache.get_cached_values("payment_method")
    assert "currency" in normalizer_cache._cache["values"]
    assert "payment_method" in normalizer_cache._cache["values"]


# ---------------------------------------------------------------------------
# get_cached_mapping
# ---------------------------------------------------------------------------

def test_get_cached_mapping_nonexistent_returns_none():
    from normalizer_cache import get_cached_mapping
    result = get_cached_mapping("__nonexistent_template__")
    assert result is None


def test_get_cached_mapping_stored_in_cache():
    import normalizer_cache
    normalizer_cache.get_cached_mapping("__nonexistent_template__")
    assert "__nonexistent_template__" in normalizer_cache._cache["mappings"]


# ---------------------------------------------------------------------------
# get_cached_categories
# ---------------------------------------------------------------------------

def test_get_cached_categories_returns_list():
    from normalizer_cache import get_cached_categories
    cats = get_cached_categories()
    assert isinstance(cats, list)


def test_get_cached_categories_cached():
    import normalizer_cache
    first = normalizer_cache.get_cached_categories()
    second = normalizer_cache.get_cached_categories()
    assert first is second


# ---------------------------------------------------------------------------
# invalidate_cache
# ---------------------------------------------------------------------------

def test_invalidate_cache_clears_all():
    import normalizer_cache
    normalizer_cache.get_cached_fields()
    normalizer_cache.get_cached_categories()
    normalizer_cache.invalidate_cache()
    assert normalizer_cache._cache["fields"] is None
    assert normalizer_cache._cache["categories"] is None
    assert normalizer_cache._meta["version_hash"] is None


# ---------------------------------------------------------------------------
# NORMALIZE_CACHE_ENABLED toggle
# ---------------------------------------------------------------------------

def test_cache_disabled_always_queries_db(monkeypatch):
    import normalizer_cache
    import settings
    monkeypatch.setattr(settings, "NORMALIZE_CACHE_ENABLED", False)
    monkeypatch.setattr(normalizer_cache, "NORMALIZE_CACHE_ENABLED", False)
    # Should still return data even when cache is disabled
    result = normalizer_cache.get_cached_fields()
    assert isinstance(result, list)
    # Cache should NOT be populated
    assert normalizer_cache._cache["fields"] is None


# ---------------------------------------------------------------------------
# Version hash
# ---------------------------------------------------------------------------

def test_version_hash_is_sha256():
    import normalizer_cache
    normalizer_cache.get_cached_fields()
    h = normalizer_cache._meta["version_hash"]
    assert h is not None
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)
