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
    first = _server.create_success_response("# first", meta={"quality_score": 0.42})
    second = _server.create_success_response("# second", meta={"quality_score": 0.91})

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
    assert result.meta.retry_applied is True
    assert result.meta.retry_reason == "low_quality"
    assert result.meta.initial_mode == "default"
    assert result.meta.final_mode == "full"
    assert result.meta.initial_quality_score == 0.42
    assert result.meta.final_quality_score == 0.91
    assert result.meta.retry_threshold_used == 0.75


def test_retry_skipped_when_quality_is_high_enough():
    first = _server.create_success_response("# first", meta={"quality_score": 0.88})

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
    first = _server.create_success_response("# first", meta={"quality_score": 0.42})

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
    first = _server.create_success_response("# first", meta={"quality_score": None})
    second = _server.create_success_response("# second", meta={"quality_score": 0.81})

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
    first = _server.create_success_response("# first", meta={"quality_score": 0.42})
    second = _server.create_success_response("# second", meta={"quality_score": 0.90})

    with patch.object(_server, "QUALITY_RETRY_ENABLED", True), \
         patch.object(_server, "QUALITY_RETRY_THRESHOLD", 0.75), \
         patch.object(_server, "QUALITY_RETRY_MODE", "full"), \
         patch.object(_server, "_convert_auto_impl", new=AsyncMock(side_effect=[first, second])) as impl:
        result = run_async(_server.convert_auto(**_kwargs()))

    assert result.markdown == "# second"
    assert impl.await_count == 2
    assert result.meta.retry_applied is True
