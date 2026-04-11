"""
Tests for E10/W10.1/T10.1.1: baseline success response contract.

These tests lock the minimum response shape and the canonical meta fields so
integrators can rely on null instead of field drift.
"""

import json

from models import (
    ConvertResponse,
    MetaData,
    MINIMUM_META_FIELDS,
    SUCCESS_RESPONSE_DEFAULTS,
    create_success_response,
)


def test_create_success_response_materializes_all_success_defaults():
    """Successful responses must carry the stable top-level shape."""
    response = create_success_response("# Test")
    data = response.model_dump()

    assert data["success"] is True
    assert data["markdown"] == "# Test"

    for field_name, expected in SUCCESS_RESPONSE_DEFAULTS.items():
        assert field_name in data, f"Missing top-level field: {field_name}"
        assert data[field_name] == expected


def test_create_success_response_materializes_canonical_meta_fields():
    """Canonical meta fields must exist even when their value is unknown."""
    response = create_success_response("# Test")
    meta = response.model_dump()["meta"]

    for field_name, expected in MINIMUM_META_FIELDS.items():
        assert field_name in meta, f"Missing canonical meta field: {field_name}"
        assert meta[field_name] == expected


def test_create_success_response_preserves_explicit_canonical_meta_values():
    """Caller-provided canonical values must override the baseline nulls."""
    response = create_success_response(
        "# Test",
        meta={
            "document_type": "invoice",
            "document_type_confidence": 0.97,
            "template_used": "invoice",
            "template_version": 3,
            "quality_score": 0.88,
            "quality_grade": "good",
            "accuracy_mode": "standard",
            "pipeline_steps": ["convert", "classify"],
        },
    )
    meta = response.model_dump()["meta"]

    assert meta["document_type"] == "invoice"
    assert meta["document_type_confidence"] == 0.97
    assert meta["template_used"] == "invoice"
    assert meta["template_version"] == 3
    assert meta["quality_score"] == 0.88
    assert meta["quality_grade"] == "good"
    assert meta["accuracy_mode"] == "standard"
    assert meta["pipeline_steps"] == ["convert", "classify"]


def test_success_response_json_contains_nulls_instead_of_missing_fields():
    """The serialized API shape must keep known fields as null, not omit them."""
    payload = json.loads(create_success_response("# Test").model_dump_json())

    assert payload["html"] is None
    assert payload["extracted"] is None
    assert payload["chunks"] is None
    assert payload["normalized"] is None
    assert payload["meta"]["document_type"] is None
    assert payload["meta"]["template_used"] is None
    assert payload["meta"]["quality_score"] is None
    assert payload["meta"]["pipeline_steps"] is None


def test_convert_response_direct_model_still_serializes_stable_null_shape():
    """Direct model construction must remain compatible with the baseline shape."""
    response = ConvertResponse(success=True, markdown="# Test", meta=MetaData())
    payload = response.model_dump()

    for field_name in SUCCESS_RESPONSE_DEFAULTS:
        assert field_name in payload

    for field_name in MINIMUM_META_FIELDS:
        assert field_name in payload["meta"]
