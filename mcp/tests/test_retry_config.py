"""
Tests for E10/W10.3/T10.3.1 low-quality retry request/env contract.
"""

import pytest

from conftest import load_server_module, run_async


_server = load_server_module(use_real_pil=False)


def test_convert_request_accepts_low_quality_retry_fields():
    from models import ConvertRequest

    req = ConvertRequest(
        path="/data/test.pdf",
        retry_on_low_quality=True,
        quality_retry_threshold=0.81,
        quality_retry_mode="full",
    )

    assert req.retry_on_low_quality is True
    assert req.quality_retry_threshold == 0.81
    assert req.quality_retry_mode == "full"


def test_extract_request_accepts_low_quality_retry_fields():
    from models import ExtractRequest

    req = ExtractRequest(
        path="/data/test.pdf",
        extract_schema={"type": "object"},
        retry_on_low_quality=False,
        quality_retry_threshold=0.67,
        quality_retry_mode="full",
    )

    assert req.retry_on_low_quality is False
    assert req.quality_retry_threshold == 0.67
    assert req.quality_retry_mode == "full"


def test_invalid_quality_retry_mode_is_rejected():
    from models import ConvertRequest

    with pytest.raises(ValueError):
        ConvertRequest(path="/data/test.pdf", quality_retry_mode="deep")


def test_convert_auto_preserves_unset_retry_fields_until_impl(monkeypatch):
    captured = {}

    async def fake_impl(**kwargs):
        captured.update(kwargs)
        return _server.create_success_response("# ok")

    monkeypatch.setattr(_server, "_convert_auto_impl", fake_impl)

    result = run_async(_server.convert_auto(
        file_data=b"test",
        filename="test.txt",
        source="base64",
        source_type="base64",
        input_meta={},
    ))

    assert result.success is True
    assert captured["retry_on_low_quality"] is None
    assert captured["quality_retry_threshold"] is None
    assert captured["quality_retry_mode"] is None
