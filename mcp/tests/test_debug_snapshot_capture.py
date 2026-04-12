from unittest.mock import AsyncMock, patch

from conftest import PNG_100x100, load_server_module, run_async


_server = load_server_module(use_real_pil=False)


def _make_vision_response(content: str, tokens_total: int = 50) -> dict:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {
            "prompt_tokens": 20,
            "completion_tokens": 30,
            "total_tokens": tokens_total,
        },
    }


def test_convert_auto_persists_debug_snapshot_when_policy_matches():
    vision_resp = _make_vision_response("# Snapshot Contract")

    with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
         patch.object(_server, "call_mistral_vision_api", new=AsyncMock(return_value=vision_resp)), \
         patch.object(_server, "should_capture_debug_snapshot", return_value=True), \
         patch.object(_server, "build_debug_snapshot_payload", side_effect=lambda **kwargs: kwargs) as payload_mock, \
         patch.object(_server, "debug_snapshot_store", return_value=4242) as store_mock:
        result = run_async(
            _server.convert_auto(
                file_data=PNG_100x100,
                filename="snapshot.png",
                source="snapshot.png",
                source_type="base64",
                input_meta={},
                language="de",
                no_cache=True,
            )
        )

    assert result.success is True
    assert result.meta.debug_snapshot_id == 4242
    assert store_mock.call_args.kwargs["stage"] == "convert_result"
    assert store_mock.call_args.kwargs["request_id"] == result.meta.request_id
    assert payload_mock.call_args.kwargs["markdown"] == "# Snapshot Contract"


def test_build_debug_snapshot_payload_supports_extract_stage():
    payload = _server.build_debug_snapshot_payload(
        request_id="req-extract",
        job_id="job-extract",
        filename="extract.pdf",
        source_type="base64",
        stage="extract_result",
        attempt_number=1,
        attempt_count=1,
        attempt_mode="default",
        meta={"template_used": "invoice"},
        extracted={"invoice_number": "INV-1"},
    )

    assert payload["stage"] == "extract_result"
    assert payload["extracted"]["invoice_number"] == "INV-1"


def test_build_debug_snapshot_payload_supports_normalized_stage():
    payload = _server.build_debug_snapshot_payload(
        request_id="req-normalized",
        job_id="job-normalized",
        filename="normalized.pdf",
        source_type="base64",
        stage="normalized_result",
        attempt_number=2,
        attempt_count=2,
        attempt_mode="full",
        meta={"template_used": "invoice"},
        normalized={"invoice_number": "INV-1"},
    )

    assert payload["stage"] == "normalized_result"
    assert payload["normalized"]["invoice_number"] == "INV-1"


def test_build_debug_snapshot_payload_supports_error_stage():
    payload = _server.build_debug_snapshot_payload(
        request_id="req-error",
        job_id="job-error",
        filename="error.pdf",
        source_type="base64",
        stage="error_result",
        attempt_number=2,
        attempt_count=2,
        attempt_mode="full",
        meta={"template_used": "invoice"},
        error="boom",
    )

    assert payload["stage"] == "error_result"
    assert payload["error"] == "boom"
