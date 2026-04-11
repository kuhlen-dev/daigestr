"""
Tests for E10/W10.4/T10.4.5 score semantics separation.
"""

import asyncio

from conftest import load_server_module


_server = load_server_module(use_real_pil=False)


def test_normalized_context_includes_document_quality_score(monkeypatch):
    import normalizer as normalizer_mod

    monkeypatch.setattr(
        normalizer_mod,
        "get_cached_mapping",
        lambda template_name: {"normalize_mapping": {"summary": "_meta.zusammenfassung"}, "required_normalized_fields": []},
    )
    monkeypatch.setattr(
        normalizer_mod,
        "get_cached_fields",
        lambda: [{"name": "summary", "target_type": "text"}],
    )

    result = asyncio.get_event_loop().run_until_complete(
        normalizer_mod.normalize(
            extracted={"_meta": {"zusammenfassung": "Kurzfassung"}},
            template_name="invoice",
            meta={"quality_score": 0.87},
            compact=False,
        )
    )

    assert result["normalized_context"]["document_quality_score"] == 0.87
    assert result["normalized_context"]["quality_score"] == result["quality_score"]
