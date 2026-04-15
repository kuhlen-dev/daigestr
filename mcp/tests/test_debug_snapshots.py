import importlib
import os
import sys


def _reload_debug_snapshots(monkeypatch, **env):
    for key in [
        "DEBUG_SNAPSHOTS_ENABLED",
        "DEBUG_SNAPSHOTS_RETENTION_DAYS",
        "DEBUG_SNAPSHOTS_POLICIES",
        "DEBUG_SNAPSHOTS_LONG_DOCUMENT_PAGE_THRESHOLD",
        "DEBUG_SNAPSHOTS_LOW_QUALITY_THRESHOLD",
        "DEBUG_SNAPSHOTS_INCLUDE_MARKDOWN",
        "DEBUG_SNAPSHOTS_INCLUDE_EXTRACTED",
        "DEBUG_SNAPSHOTS_INCLUDE_NORMALIZED",
        "DEBUG_SNAPSHOTS_INCLUDE_ERRORS",
        "DEBUG_SNAPSHOTS_ALLOW_PII",
        "PII_STORAGE_MODE",
        "PII_SENSITIVE_FIELDS",
    ]:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    if "settings" in sys.modules:
        importlib.reload(sys.modules["settings"])
    else:
        importlib.import_module("settings")

    if "debug_snapshots" in sys.modules:
        return importlib.reload(sys.modules["debug_snapshots"])
    return importlib.import_module("debug_snapshots")


def test_should_capture_debug_snapshot_disabled_by_default(monkeypatch):
    module = _reload_debug_snapshots(monkeypatch)

    assert module.should_capture_debug_snapshot(
        success=False,
        quality_score=0.2,
        page_count=100,
        retry_applied=True,
    ) is False


def test_should_capture_debug_snapshot_matches_failure_retry_and_quality(monkeypatch):
    module = _reload_debug_snapshots(
        monkeypatch,
        DEBUG_SNAPSHOTS_ENABLED="true",
        DEBUG_SNAPSHOTS_POLICIES="failures,retries,low_quality",
        DEBUG_SNAPSHOTS_LOW_QUALITY_THRESHOLD="0.75",
    )

    assert module.should_capture_debug_snapshot(
        success=False,
        quality_score=0.9,
        page_count=1,
        retry_applied=False,
    ) is True
    assert module.should_capture_debug_snapshot(
        success=True,
        quality_score=0.9,
        page_count=1,
        retry_applied=True,
    ) is True
    assert module.should_capture_debug_snapshot(
        success=True,
        quality_score=0.6,
        page_count=1,
        retry_applied=False,
    ) is True


def test_should_capture_debug_snapshot_matches_long_documents(monkeypatch):
    module = _reload_debug_snapshots(
        monkeypatch,
        DEBUG_SNAPSHOTS_ENABLED="true",
        DEBUG_SNAPSHOTS_POLICIES="long_documents",
        DEBUG_SNAPSHOTS_LONG_DOCUMENT_PAGE_THRESHOLD="20",
    )

    assert module.should_capture_debug_snapshot(
        success=True,
        quality_score=0.9,
        page_count=25,
        retry_applied=False,
    ) is True
    assert module.should_capture_debug_snapshot(
        success=True,
        quality_score=0.9,
        page_count=3,
        retry_applied=False,
    ) is False


def test_build_debug_snapshot_payload_respects_include_flags(monkeypatch):
    module = _reload_debug_snapshots(
        monkeypatch,
        DEBUG_SNAPSHOTS_ENABLED="true",
        DEBUG_SNAPSHOTS_INCLUDE_MARKDOWN="false",
        DEBUG_SNAPSHOTS_INCLUDE_EXTRACTED="true",
        DEBUG_SNAPSHOTS_INCLUDE_NORMALIZED="false",
        DEBUG_SNAPSHOTS_INCLUDE_ERRORS="true",
        DEBUG_SNAPSHOTS_RETENTION_DAYS="21",
        DEBUG_SNAPSHOTS_ALLOW_PII="true",
    )

    payload = module.build_debug_snapshot_payload(
        request_id="req-1",
        job_id="job-1",
        filename="scan.pdf",
        source_type="base64",
        stage="extract",
        attempt_number=2,
        attempt_count=2,
        attempt_mode="full",
        meta={"quality_score": 0.72},
        markdown="# hidden",
        extracted={"foo": "bar"},
        normalized={"norm": 1},
        error="boom",
    )

    assert payload["request_id"] == "req-1"
    assert payload["retention_days"] == 21
    assert "markdown" not in payload
    assert payload["extracted"] == {"foo": "bar"}
    assert "normalized" not in payload
    assert payload["error"] == "boom"


def test_build_debug_snapshot_payload_suppresses_sensitive_branches_in_strict_mode(monkeypatch):
    module = _reload_debug_snapshots(
        monkeypatch,
        DEBUG_SNAPSHOTS_ENABLED="true",
        DEBUG_SNAPSHOTS_INCLUDE_MARKDOWN="true",
        DEBUG_SNAPSHOTS_INCLUDE_EXTRACTED="true",
        DEBUG_SNAPSHOTS_INCLUDE_NORMALIZED="true",
        DEBUG_SNAPSHOTS_INCLUDE_ERRORS="true",
        PII_STORAGE_MODE="strict",
        DEBUG_SNAPSHOTS_ALLOW_PII="false",
    )

    payload = module.build_debug_snapshot_payload(
        request_id="req-2",
        job_id="job-2",
        filename="invoice.pdf",
        source_type="file",
        stage="normalized_result",
        attempt_number=1,
        attempt_count=1,
        attempt_mode="default",
        meta={"quality_score": 0.91},
        markdown="# sensitive",
        extracted={"iban": "DE123"},
        normalized={"amount": 12},
        error=None,
    )

    assert payload["pii_storage_mode"] == "strict"
    assert payload["pii_payloads_included"] is False
    assert "markdown" not in payload
    assert "extracted" not in payload
    assert "normalized" not in payload


def test_build_debug_snapshot_payload_allows_sensitive_branches_when_explicitly_enabled(monkeypatch):
    module = _reload_debug_snapshots(
        monkeypatch,
        DEBUG_SNAPSHOTS_ENABLED="true",
        DEBUG_SNAPSHOTS_INCLUDE_MARKDOWN="true",
        DEBUG_SNAPSHOTS_INCLUDE_EXTRACTED="true",
        DEBUG_SNAPSHOTS_INCLUDE_NORMALIZED="true",
        PII_STORAGE_MODE="strict",
        DEBUG_SNAPSHOTS_ALLOW_PII="true",
    )

    payload = module.build_debug_snapshot_payload(
        request_id="req-3",
        job_id="job-3",
        filename="invoice.pdf",
        source_type="file",
        stage="normalized_result",
        attempt_number=1,
        attempt_count=1,
        attempt_mode="default",
        meta={"quality_score": 0.91},
        markdown="# visible",
        extracted={"iban": "DE123"},
        normalized={"amount": 12},
        error=None,
    )

    assert payload["pii_payloads_included"] is True
    assert payload["markdown"] == "# visible"
    assert payload["extracted"] == {"iban": "DE123"}
    assert payload["normalized"] == {"amount": 12}


def test_invalid_debug_snapshot_policy_raises(monkeypatch):
    monkeypatch.setenv("DEBUG_SNAPSHOTS_POLICIES", "failures,unknown")

    if "settings" in sys.modules:
        del sys.modules["settings"]

    try:
        importlib.import_module("settings")
        raise AssertionError("Expected invalid debug snapshot policy to raise ValueError")
    except ValueError as exc:
        assert "DEBUG_SNAPSHOTS_POLICIES" in str(exc)
