"""
Regression tests for cache-hit response contract stability.
"""

from conftest import load_server_module, run_async


class TestCacheContract:
    """Cache hits must preserve the canonical meta shape."""

    @classmethod
    def setup_class(cls):
        cls.server = load_server_module(use_real_pil=False)

    def _make_common_mocks(self, monkeypatch):
        monkeypatch.setattr(self.server, "is_scanned_pdf", lambda path: False)
        monkeypatch.setattr(
            self.server,
            "convert_with_markitdown",
            lambda path, show_formulas=False: {"success": True, "markdown": "# Cached Contract"},
        )
        monkeypatch.setattr(self.server, "detect_mimetype_from_bytes", lambda data: None)

    def test_cache_hit_preserves_canonical_meta_nulls(self, monkeypatch):
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

        kwargs = dict(
            file_data=b"cache contract content",
            filename="cache.txt",
            source="base64",
            source_type="base64",
            input_meta={},
        )

        first = run_async(self.server.convert_auto(**kwargs))
        second = run_async(self.server.convert_auto(**kwargs))

        assert first.success is True
        assert second.success is True
        assert second.meta.cached is True
        assert second.meta.document_type is None
        assert second.meta.document_type_confidence is None
        assert second.meta.template_used is None
        assert second.meta.template_version is None
        assert second.meta.quality_score is not None
        assert second.meta.quality_grade is not None
        assert second.meta.accuracy_mode is not None
        assert second.meta.pipeline_steps is not None
