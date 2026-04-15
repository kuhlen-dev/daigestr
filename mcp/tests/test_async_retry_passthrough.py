import importlib
from unittest.mock import patch

from conftest import load_server_module, run_async


_server = load_server_module(use_real_pil=False)
api_rest = importlib.import_module("api_rest")


def test_async_job_passes_retry_parameters():
    captured = {}

    async def fake_convert_auto(**kwargs):
        captured.update(kwargs)
        return _server.create_success_response("ok", meta={})

    request = _server.ConvertRequest(
        base64="JVBERi0xLjQgZmFrZQ==",
        filename="sample.pdf",
        meta={},
        auto_extract=True,
        retry_on_low_quality=True,
        quality_retry_threshold=0.75,
        quality_retry_mode="full",
        mode="default",
    )

    with patch.object(_server, "convert_auto", side_effect=fake_convert_auto), \
         patch.object(_server, "job_update"), \
         patch.object(_server, "job_set_result"), \
         patch.object(api_rest, "_fire_webhook"):
        run_async(api_rest._run_async_job("job-1", request))

    assert captured["retry_on_low_quality"] is True
    assert captured["quality_retry_threshold"] == 0.75
    assert captured["quality_retry_mode"] == "full"
    assert captured["mode"] == "default"
