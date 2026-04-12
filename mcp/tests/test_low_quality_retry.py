"""
Tests for E10/W10.3/T10.3.2 low-quality retry orchestration.
"""

from unittest.mock import AsyncMock, patch

from conftest import load_server_module, run_async


_server = load_server_module(use_real_pil=False)


def _kwargs(**overrides):
    base = dict(
        file_data=b"retry test",
        filename="retry.pdf",
        source="retry.pdf",
        source_type="file",
        input_meta={},
        auto_extract=True,
    )
    base.update(overrides)
    return base


def test_retry_on_low_quality_escalates_default_to_full():
    first = _server.create_success_response("# first", meta={"quality_score": 0.42, "template_used": "invoice", "template_version": 1})
    first.extracted = {"invoice_number": "INV-1"}
    second = _server.create_success_response("# second", meta={"quality_score": 0.91, "template_used": "invoice", "template_version": 1})
    second.extracted = {"invoice_number": "INV-1"}

    with patch.object(_server, "_convert_auto_impl", new=AsyncMock(side_effect=[first, second])) as impl:
        result = run_async(_server.convert_auto(
            **_kwargs(
                retry_on_low_quality=True,
                quality_retry_threshold=0.75,
                quality_retry_mode="full",
            )
        ))

    assert result.markdown == "# second"
    assert impl.await_count == 2
    assert impl.await_args_list[0].kwargs["mode"] == "default"
    assert impl.await_args_list[1].kwargs["mode"] == "full"
    assert impl.await_args_list[0].kwargs["request_id"] == impl.await_args_list[1].kwargs["request_id"]
    assert impl.await_args_list[0].kwargs["attempt_number"] == 1
    assert impl.await_args_list[1].kwargs["attempt_number"] == 2
    assert result.meta.retry_applied is True
    assert result.meta.retry_reason == "low_quality"
    assert result.meta.initial_mode == "default"
    assert result.meta.final_mode == "full"
    assert result.meta.initial_quality_score == 0.42
    assert result.meta.final_quality_score == 0.91
    assert result.meta.retry_threshold_used == 0.75
    assert result.meta.request_id == impl.await_args_list[0].kwargs["request_id"]
    assert result.meta.attempt_count == 2
    assert result.meta.attempt_mode == "full"


def test_retry_skipped_when_quality_is_high_enough():
    first = _server.create_success_response("# first", meta={"quality_score": 0.88, "template_used": "invoice", "template_version": 1})
    first.extracted = {"invoice_number": "INV-1"}

    with patch.object(_server, "_convert_auto_impl", new=AsyncMock(return_value=first)) as impl:
        result = run_async(_server.convert_auto(
            **_kwargs(
                retry_on_low_quality=True,
                quality_retry_threshold=0.75,
                quality_retry_mode="full",
            )
        ))

    assert result.markdown == "# first"
    assert impl.await_count == 1
    assert result.meta.retry_applied is False
    assert result.meta.retry_reason is None
    assert result.meta.initial_mode == "default"
    assert result.meta.final_mode == "default"
    assert result.meta.initial_quality_score == 0.88
    assert result.meta.final_quality_score == 0.88


def test_retry_skipped_when_mode_already_full():
    first = _server.create_success_response("# first", meta={"quality_score": 0.42, "template_used": "invoice", "template_version": 1})
    first.extracted = {"invoice_number": "INV-1"}

    with patch.object(_server, "_convert_auto_impl", new=AsyncMock(return_value=first)) as impl:
        result = run_async(_server.convert_auto(
            **_kwargs(
                mode="full",
                retry_on_low_quality=True,
                quality_retry_threshold=0.75,
                quality_retry_mode="full",
            )
        ))

    assert result.markdown == "# first"
    assert impl.await_count == 1
    assert result.meta.retry_applied is False
    assert result.meta.initial_mode == "full"
    assert result.meta.final_mode == "full"


def test_retry_triggers_on_missing_quality_score():
    first = _server.create_success_response("# first", meta={"quality_score": None, "template_used": "invoice", "template_version": 1})
    first.extracted = {"invoice_number": "INV-1"}
    second = _server.create_success_response("# second", meta={"quality_score": 0.81, "template_used": "invoice", "template_version": 1})
    second.extracted = {"invoice_number": "INV-1"}

    with patch.object(_server, "_convert_auto_impl", new=AsyncMock(side_effect=[first, second])) as impl:
        result = run_async(_server.convert_auto(
            **_kwargs(
                retry_on_low_quality=True,
                quality_retry_threshold=0.75,
                quality_retry_mode="full",
            )
        ))

    assert result.markdown == "# second"
    assert impl.await_count == 2
    assert result.meta.retry_applied is True
    assert result.meta.retry_reason == "missing_quality_score"


def test_retry_uses_env_defaults_when_request_omits_overrides():
    first = _server.create_success_response("# first", meta={"quality_score": 0.42, "template_used": "invoice", "template_version": 1})
    first.extracted = {"invoice_number": "INV-1"}
    second = _server.create_success_response("# second", meta={"quality_score": 0.90, "template_used": "invoice", "template_version": 1})
    second.extracted = {"invoice_number": "INV-1"}

    with patch.object(_server, "QUALITY_RETRY_ENABLED", True), \
         patch.object(_server, "QUALITY_RETRY_THRESHOLD", 0.75), \
         patch.object(_server, "QUALITY_RETRY_MODE", "full"), \
         patch.object(_server, "_convert_auto_impl", new=AsyncMock(side_effect=[first, second])) as impl:
        result = run_async(_server.convert_auto(**_kwargs()))

    assert result.markdown == "# second"
    assert impl.await_count == 2
    assert result.meta.retry_applied is True


def test_retry_keeps_initial_result_when_retry_contract_is_incomplete():
    first = _server.create_success_response(
        "# first",
        meta={"quality_score": 0.42, "template_used": "bank_statement", "template_version": 1},
    )
    first.extracted = {"auszugsnummer": "11", "buchungen": [{"datum": "2024-08-19"}]}
    second = _server.create_success_response("# second", meta={"quality_score": 0.90})
    second.extracted = None

    with patch.object(_server, "_convert_auto_impl", new=AsyncMock(side_effect=[first, second])) as impl:
        result = run_async(_server.convert_auto(
            **_kwargs(
                retry_on_low_quality=True,
                quality_retry_threshold=0.75,
                quality_retry_mode="full",
            )
        ))

    assert impl.await_count == 2
    assert result.success is True
    assert result.markdown == "# first"
    assert result.extracted == first.extracted
    assert result.meta.template_used == "bank_statement"
    assert result.meta.retry_applied is True
    assert result.meta.final_mode == "default"


def test_retry_fails_when_both_attempts_violate_extraction_contract():
    first = _server.create_success_response("# first", meta={"quality_score": 0.42})
    first.extracted = None
    second = _server.create_success_response("# second", meta={"quality_score": 0.90})
    second.extracted = None

    with patch.object(_server, "_convert_auto_impl", new=AsyncMock(side_effect=[first, second])) as impl:
        result = run_async(_server.convert_auto(
            **_kwargs(
                retry_on_low_quality=True,
                quality_retry_threshold=0.75,
                quality_retry_mode="full",
            )
        ))

    assert impl.await_count == 2
    assert result.success is False
    assert result.error is not None
    assert result.error.code == "CONVERSION_FAILED"
    assert result.meta.retry_applied is True
