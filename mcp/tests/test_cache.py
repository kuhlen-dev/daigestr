"""
Tests für T-DAI-019: Request-Level-Cache (Hash-basiert).

Prüft:
- Gleicher Request 2x → 2. Mal meta.cached=True
- Anderer Parameter → kein Cache-Hit
- Cache-Clear → kein Hit mehr
- CACHE_ENABLED=false → kein Caching
"""

import os
import sys
import json
import hashlib
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch, call

import psycopg2
import pytest

sys.path.insert(0, str(Path(__file__).parent))
from conftest import load_server_module, run_async

_DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://daigestr:daigestr@localhost:15432/daigestr",
)


def _make_minimal_pdf_bytes() -> bytes:
    """Minimalste PDF-Bytes die als Dokument erkannt werden."""
    return b"%PDF-1.4 test content for cache test"


def _make_convert_response_json(markdown: str = "# Test\n\nContent", cached: bool = False) -> str:
    """Erstellt ein gültiges ConvertResponse JSON für Cache-Tests."""
    meta = {
        "source": "base64",
        "source_type": "base64",
        "format": "txt",
        "size_bytes": 100,
        "duration_ms": 50,
        "accuracy_mode": "standard",
    }
    if cached:
        meta["cached"] = True
    return json.dumps({
        "success": True,
        "markdown": markdown,
        "meta": meta,
        "error": None,
        "extracted": None,
        "chunks": None,
        "enriched_pdf": None,
        "html": None,
    })


class TestCacheSettings:
    """Prüft CACHE_TTL_SECONDS und CACHE_ENABLED ENV-Variablen."""

    def test_cache_ttl_default(self):
        server = load_server_module(isolate_runtime_state=False)
        assert server.CACHE_TTL_SECONDS == 3600
        assert isinstance(server.CACHE_TTL_SECONDS, int)

    def test_cache_enabled_default(self):
        server = load_server_module(isolate_runtime_state=False)
        assert server.CACHE_ENABLED is True

    def test_cache_ttl_override(self):
        with patch.dict(os.environ, {"CACHE_TTL_SECONDS": "600"}):
            server = load_server_module(isolate_runtime_state=False)
            assert server.CACHE_TTL_SECONDS == 600

    def test_cache_enabled_false(self):
        with patch.dict(os.environ, {"CACHE_ENABLED": "false"}):
            server = load_server_module(isolate_runtime_state=False)
            assert server.CACHE_ENABLED is False

    def test_cache_enabled_case_insensitive(self):
        with patch.dict(os.environ, {"CACHE_ENABLED": "FALSE"}):
            server = load_server_module(isolate_runtime_state=False)
            assert server.CACHE_ENABLED is False


class TestCacheFunctions:
    """Prüft cache_get, cache_set, cache_clear in templates_db."""

    def setup_method(self):
        self.server = load_server_module()
        import templates_db as _tdb
        _tdb.pool_reset()
        conn = psycopg2.connect(_DB_URL)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("TRUNCATE TABLE cache")
        conn.close()
        _tdb.pool_reset()

    def test_cache_get_miss(self):
        """Cache-Miss gibt None zurück."""
        result = self.server.cache_get("nonexistent_key", 3600)
        assert result is None

    def test_cache_set_and_get(self):
        """Gespeicherte Response wird korrekt zurückgegeben."""
        test_json = '{"success": true, "markdown": "# Test"}'
        self.server.cache_set("test_key", test_json)
        result = self.server.cache_get("test_key", 3600)
        assert result == test_json

    def test_cache_clear(self):
        """Nach cache_clear gibt cache_get None zurück."""
        self.server.cache_set("key1", '{"success": true}')
        self.server.cache_set("key2", '{"success": true}')

        # Vor dem Clear: Hit
        assert self.server.cache_get("key1", 3600) is not None
        assert self.server.cache_get("key2", 3600) is not None

        self.server.cache_clear()

        # Nach dem Clear: Miss
        assert self.server.cache_get("key1", 3600) is None
        assert self.server.cache_get("key2", 3600) is None


class TestCacheHitInConvertAuto:
    """Prüft Cache-Integration in convert_auto."""

    def setup_method(self):
        self.server = load_server_module()

    def _make_common_mocks(self, monkeypatch):
        """Setzt gemeinsame Mocks für convert_auto Tests auf."""
        def mock_is_scanned(path):
            return False

        def mock_convert_markitdown(path, show_formulas=False):
            return {"success": True, "markdown": "# Converted Result"}

        def mock_detect_mimetype(data):
            return None  # Kein Mimetype-Override → Extension-Routing greift

        monkeypatch.setattr(self.server, "is_scanned_pdf", mock_is_scanned)
        monkeypatch.setattr(self.server, "convert_with_markitdown", mock_convert_markitdown)
        monkeypatch.setattr(self.server, "detect_mimetype_from_bytes", mock_detect_mimetype)

    def test_second_request_returns_cached(self, monkeypatch):
        """Erster Request speichert im Cache; zweiter identischer Request → meta.cached=True."""
        stored_cache: dict[str, str] = {}

        def mock_cache_get(key, ttl):
            return stored_cache.get(key)

        def mock_cache_set(key, response_json):
            stored_cache[key] = response_json

        monkeypatch.setattr(self.server, "CACHE_ENABLED", True)
        monkeypatch.setattr(self.server, "CACHE_TTL_SECONDS", 3600)
        monkeypatch.setattr(self.server, "cache_get", mock_cache_get)
        monkeypatch.setattr(self.server, "cache_set", mock_cache_set)
        self._make_common_mocks(monkeypatch)

        file_data = b"test content for caching"

        # Erster Aufruf: Cache-Miss → konvertiert → speichert im Cache
        result1 = run_async(self.server.convert_auto(
            file_data=file_data,
            filename="test.txt",
            source="base64",
            source_type="base64",
            input_meta={},
        ))

        # Nach erstem Aufruf: Cache sollte befüllt sein
        assert len(stored_cache) == 1
        assert result1.success is True
        assert not result1.meta.cached  # Erster Aufruf ist nicht gecacht

        # Zweiter identischer Aufruf: Cache-Hit → meta.cached=True
        result2 = run_async(self.server.convert_auto(
            file_data=file_data,
            filename="test.txt",
            source="base64",
            source_type="base64",
            input_meta={},
        ))

        assert result2.success is True
        assert result2.meta.cached is True

    def test_different_parameter_no_cache_hit(self, monkeypatch):
        """Anderer Parameter (z.B. language) → anderer Cache-Key → kein Hit."""
        stored_cache: dict[str, str] = {}

        def mock_cache_get(key, ttl):
            return stored_cache.get(key)

        def mock_cache_set(key, response_json):
            stored_cache[key] = response_json

        monkeypatch.setattr(self.server, "CACHE_ENABLED", True)
        monkeypatch.setattr(self.server, "CACHE_TTL_SECONDS", 3600)
        monkeypatch.setattr(self.server, "cache_get", mock_cache_get)
        monkeypatch.setattr(self.server, "cache_set", mock_cache_set)
        self._make_common_mocks(monkeypatch)

        file_data = b"content for key differentiation test"

        # Erster Request mit language="de"
        result1 = run_async(self.server.convert_auto(
            file_data=file_data,
            filename="doc.txt",
            source="base64",
            source_type="base64",
            input_meta={},
            language="de",
        ))

        # Zweiter Request mit language="en" → anderer Key → kein Hit
        result2 = run_async(self.server.convert_auto(
            file_data=file_data,
            filename="doc.txt",
            source="base64",
            source_type="base64",
            input_meta={},
            language="en",
        ))

        # Beide erfolgreich
        assert result1.success is True
        assert result2.success is True
        # result2 wurde NICHT aus Cache geholt (anderer Key)
        assert not result2.meta.cached
        # Zwei verschiedene Keys im Cache
        assert len(stored_cache) == 2

    def test_cache_disabled_no_caching(self, monkeypatch):
        """CACHE_ENABLED=False → cache_get und cache_set werden NICHT aufgerufen."""
        cache_get_called = {"n": 0}
        cache_set_called = {"n": 0}

        def mock_cache_get(key, ttl):
            cache_get_called["n"] += 1
            return None

        def mock_cache_set(key, response_json):
            cache_set_called["n"] += 1

        monkeypatch.setattr(self.server, "CACHE_ENABLED", False)
        monkeypatch.setattr(self.server, "cache_get", mock_cache_get)
        monkeypatch.setattr(self.server, "cache_set", mock_cache_set)
        self._make_common_mocks(monkeypatch)

        run_async(self.server.convert_auto(
            file_data=b"content when disabled",
            filename="test.txt",
            source="base64",
            source_type="base64",
            input_meta={},
        ))

        assert cache_get_called["n"] == 0
        assert cache_set_called["n"] == 0

    def test_cache_clear_then_no_hit(self, monkeypatch):
        """Nach cache_clear kein Cache-Hit mehr."""
        import templates_db as _tdb
        _tdb.pool_reset()
        conn = psycopg2.connect(_DB_URL)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("TRUNCATE TABLE cache")
        conn.close()
        _tdb.pool_reset()

        monkeypatch.setattr(self.server, "CACHE_ENABLED", True)
        monkeypatch.setattr(self.server, "CACHE_TTL_SECONDS", 3600)

        # Cache befüllen
        test_key = "test_clear_key"
        self.server.cache_set(test_key, _make_convert_response_json())

        # Vor dem Clear: Hit
        assert self.server.cache_get(test_key, 3600) is not None

        # Clear
        self.server.cache_clear()

        # Nach dem Clear: Miss
        assert self.server.cache_get(test_key, 3600) is None
