import json
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


def test_convert_auto_job_progress_uses_canonical_shape():
    updates: list[dict] = []
    vision_resp = _make_vision_response("# Progress")

    def fake_job_update(job_id, status, progress_json=None):
        updates.append(
            {
                "job_id": job_id,
                "status": status,
                "progress": json.loads(progress_json) if progress_json else None,
            }
        )

    with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
         patch.object(_server, "call_mistral_vision_api", new=AsyncMock(return_value=vision_resp)), \
         patch.object(_server, "job_update", side_effect=fake_job_update):
        result = run_async(
            _server.convert_auto(
                file_data=PNG_100x100,
                filename="progress.png",
                source="progress.png",
                source_type="base64",
                input_meta={"_job_id": "job-123"},
                language="de",
                no_cache=True,
            )
        )

    assert result.success is True
    assert updates
    first = updates[0]
    assert first["job_id"] == "job-123"
    assert first["status"] == "processing"
    assert first["progress"]["status"] == "processing"
    assert first["progress"]["current_stage"] == "start"
    assert first["progress"]["message"] == "Starting conversion"
    assert first["progress"]["percent"] == 0
    assert first["progress"]["job_id"] == "job-123"
    assert first["progress"]["request_id"] == result.meta.request_id
    assert first["progress"]["attempt_mode"] == "default"
    assert first["progress"]["metadata"]["filename"] == "progress.png"
