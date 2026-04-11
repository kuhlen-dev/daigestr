from unittest.mock import patch

from conftest import load_server_module, run_async


_server = load_server_module(use_real_pil=False)


def test_request_id_and_attempt_meta_stay_stable_across_internal_retry():
    captured_calls = []

    async def fake_impl(**kwargs):
        captured_calls.append(
            {
                "request_id": kwargs["request_id"],
                "attempt_number": kwargs["attempt_number"],
                "attempt_count": kwargs["attempt_count"],
                "mode": kwargs["mode"],
            }
        )
        score = 0.51 if kwargs["attempt_number"] == 1 else 0.82
        return _server.create_success_response(
            "ok",
            meta={
                "request_id": kwargs["request_id"],
                "attempt_number": kwargs["attempt_number"],
                "attempt_count": kwargs["attempt_count"],
                "attempt_mode": kwargs["mode"],
                "quality_score": score,
                "quality_grade": "fair" if score < 0.75 else "good",
            },
        )

    with patch.object(_server, "_convert_auto_impl", side_effect=fake_impl):
        response = run_async(
            _server.convert_auto(
                file_data=b"dummy",
                filename="sample.pdf",
                source="/data/sample.pdf",
                source_type="file",
                input_meta={},
                auto_extract=True,
                mode="default",
                retry_on_low_quality=True,
                quality_retry_threshold=0.75,
                quality_retry_mode="full",
            )
        )

    assert len(captured_calls) == 2
    assert captured_calls[0]["request_id"] == captured_calls[1]["request_id"]
    assert captured_calls[0]["attempt_number"] == 1
    assert captured_calls[1]["attempt_number"] == 2
    assert captured_calls[0]["mode"] == "default"
    assert captured_calls[1]["mode"] == "full"

    assert response.meta.request_id == captured_calls[0]["request_id"]
    assert response.meta.attempt_number == 2
    assert response.meta.attempt_count == 2
    assert response.meta.attempt_mode == "full"
    assert response.meta.retry_applied is True
    assert response.meta.initial_mode == "default"
    assert response.meta.final_mode == "full"
