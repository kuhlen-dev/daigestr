"""
Tests für E8/W8.2/T8.2.2: Tempfile-Cleanup in Async-URL- und PDF-Slicing-Pfaden.
"""

import hashlib
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from conftest import load_server_module, run_async


_server = load_server_module(use_real_pil=False)
convert_auto = _server.convert_auto


def _load_api_server():
    fastapi_mock = MagicMock()
    app_mock = MagicMock()
    app_mock.post = lambda *a, **kw: (lambda f: f)
    app_mock.get = lambda *a, **kw: (lambda f: f)
    app_mock.exception_handler = lambda *a, **kw: (lambda f: f)
    fastapi_mock.FastAPI = MagicMock(return_value=app_mock)
    fastapi_mock.HTTPException = Exception
    fastapi_mock.Request = MagicMock()

    return load_server_module(
        use_real_pil=False,
        extra_patches={
            "fastapi": fastapi_mock,
            "fastapi.exceptions": MagicMock(),
            "fastapi.responses": MagicMock(),
        },
    )


_server_api = _load_api_server()


class TestTempfileCleanup:
    def test_async_url_job_deletes_download_tempfile(self, tmp_path):
        from models import ConvertRequest

        pdf_bytes = b"%PDF-1.4 async content"
        get_resp = MagicMock()
        get_resp.headers = {"content-type": "application/pdf"}
        get_resp.content = pdf_bytes
        get_resp.raise_for_status = MagicMock()

        client = AsyncMock()
        client.get = AsyncMock(return_value=get_resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)

        httpx_mock = MagicMock()
        httpx_mock.AsyncClient = MagicMock(return_value=client)

        deleted_paths = []

        def _record_unlink(self, missing_ok=False):
            deleted_paths.append(str(self))

        result = _server_api.create_success_response("# PDF")
        request = ConvertRequest(url="https://example.com/async.pdf")
        expected_path = tmp_path / f"url_{hashlib.md5(request.url.encode()).hexdigest()}.pdf"

        with patch.object(_server_api, "httpx", httpx_mock), \
             patch.object(_server_api, "TEMP_DIR", tmp_path), \
             patch.object(_server_api, "convert_auto", new=AsyncMock(return_value=result)), \
             patch.object(_server_api, "job_update"), \
             patch.object(_server_api, "job_set_result"), \
             patch.object(Path, "unlink", autospec=True, side_effect=_record_unlink):
            run_async(_server_api._run_async_job("job-1", request))

        assert str(expected_path) in deleted_paths

    def test_pdf_page_slicing_deletes_original_and_sliced_tempfiles(self, tmp_path):
        pdf_bytes = b"%PDF-1.4 sliced content"
        filename = "doc.pdf"
        original_path = tmp_path / f"{hashlib.md5(pdf_bytes).hexdigest()}_{filename}"
        sliced_path = tmp_path / f"sliced_{original_path.name}"

        deleted_paths = []

        def _record_unlink(self, missing_ok=False):
            deleted_paths.append(str(self))

        parse_doc = MagicMock()
        parse_doc.__len__.return_value = 3
        slice_doc = MagicMock()

        fake_fitz = SimpleNamespace(open=MagicMock(side_effect=[parse_doc, slice_doc]))
        markitdown_result = {"success": True, "markdown": "# PDF"}

        with patch.dict(sys.modules, {"fitz": fake_fitz}), \
             patch.object(_server, "TEMP_DIR", tmp_path), \
             patch.object(_server, "CACHE_ENABLED", False), \
             patch.object(_server, "detect_mimetype_from_bytes", return_value="application/pdf"), \
             patch.object(_server, "get_prompt", return_value="PDF prompt"), \
             patch.object(_server, "is_scanned_pdf", return_value=False), \
             patch.object(_server, "convert_with_markitdown", return_value=markitdown_result), \
             patch.object(Path, "unlink", autospec=True, side_effect=_record_unlink), \
             patch.object(Path, "write_bytes", return_value=None):
            run_async(
                convert_auto(
                    file_data=pdf_bytes,
                    filename=filename,
                    source="doc.pdf",
                    source_type="file",
                    input_meta={},
                    pages="1",
                )
            )

        assert str(original_path) in deleted_paths
        assert str(sliced_path) in deleted_paths
