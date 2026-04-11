"""
Tests for E10/W10.4/T10.4.3 normalizer response surface.
"""

import asyncio

from conftest import load_server_module


_server = load_server_module(use_real_pil=False)


def test_normalize_surfaces_warning_when_mapping_is_missing(monkeypatch):
    import normalizer as normalizer_mod

    monkeypatch.setattr(normalizer_mod, "get_cached_mapping", lambda template_name: None)

    result = asyncio.get_event_loop().run_until_complete(
        normalizer_mod.normalize(
            extracted={"amount": "100"},
            template_name="receipt",
            meta={},
            compact=False,
        )
    )

    assert result is not None
    assert result["normalized"] is None
    assert result["normalized_warnings"] == ["No normalize_mapping found for template 'receipt'"]


def test_apply_normalizer_keeps_skip_warning_in_response():
    import normalizer as normalizer_mod
    from routing import _apply_normalizer
    from models import ConvertResponse, MetaData

    normalizer_mod.get_cached_mapping = lambda template_name: None

    resp = ConvertResponse(success=True, markdown="# test", meta=MetaData())
    resp.extracted = {"amount": "100"}

    result = asyncio.get_event_loop().run_until_complete(
        _apply_normalizer(resp, {}, "__nonexistent_template__", False)
    )

    assert result.normalized is None
    assert result.normalized_warnings == ["No normalize_mapping found for template '__nonexistent_template__'"]
