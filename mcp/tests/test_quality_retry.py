"""
Regression tests for low-quality retry escalation.
"""

from unittest.mock import AsyncMock, patch

from conftest import load_server_module, run_async


_server = load_server_module(use_real_pil=False)
convert_auto = _server.convert_auto


class TestQualityRetryEscalation:
    """Low-quality retry should deterministically escalate from default to full."""

    def test_low_quality_retries_in_full_mode(self):
        classify_mock = AsyncMock(return_value={"document_type": "other", "document_type_confidence": 0.88})
        ocr_mock = AsyncMock(return_value={
            "success": True,
            "corrected_text": "# Corrected",
            "corrections_count": 1,
        })

        with patch.object(_server, "CACHE_ENABLED", False), \
             patch.object(_server, "convert_with_markitdown", return_value={"success": True, "markdown": "# Test"}), \
             patch.object(_server, "detect_mimetype_from_bytes", return_value=None), \
             patch.object(_server, "calculate_quality_score", side_effect=[
                 {"quality_score": 0.41, "quality_grade": "poor"},
                 {"quality_score": 0.91, "quality_grade": "excellent"},
             ]), \
             patch.object(_server, "_apply_auto_extract", new=AsyncMock(side_effect=lambda response, *args, **kwargs: response)), \
             patch.object(_server, "classify_document", new=classify_mock), \
             patch.object(_server, "correct_ocr_text", new=ocr_mock):
            result = run_async(convert_auto(
                file_data=b"retry me",
                filename="test.txt",
                source="base64",
                source_type="base64",
                input_meta={},
                mode="default",
                auto_extract=True,
                retry_on_low_quality=True,
                quality_retry_threshold=0.75,
                quality_retry_mode="full",
            ))

        assert result.success is True
        assert result.meta.quality_score == 0.91
        assert result.meta.quality_grade == "excellent"
        assert result.meta.retry_applied is True
        assert result.meta.final_mode == "full"
        assert classify_mock.await_count == 1
        assert ocr_mock.await_count == 0

    def test_good_quality_does_not_retry(self):
        classify_mock = AsyncMock(return_value={"document_type": "other", "document_type_confidence": 0.88})
        ocr_mock = AsyncMock(return_value={
            "success": True,
            "corrected_text": "# Corrected",
            "corrections_count": 1,
        })

        with patch.object(_server, "CACHE_ENABLED", False), \
             patch.object(_server, "convert_with_markitdown", return_value={"success": True, "markdown": "# Test"}), \
             patch.object(_server, "detect_mimetype_from_bytes", return_value=None), \
             patch.object(_server, "calculate_quality_score", return_value={"quality_score": 0.86, "quality_grade": "excellent"}), \
             patch.object(_server, "classify_document", new=classify_mock), \
             patch.object(_server, "correct_ocr_text", new=ocr_mock):
            result = run_async(convert_auto(
                file_data=b"stay put",
                filename="test.txt",
                source="base64",
                source_type="base64",
                input_meta={},
                mode="default",
                retry_on_low_quality=True,
                quality_retry_threshold=0.75,
                quality_retry_mode="full",
            ))

        assert result.success is True
        assert result.meta.quality_score == 0.86
        assert result.meta.accuracy_mode == "standard"
        assert classify_mock.await_count == 0
        assert ocr_mock.await_count == 0
