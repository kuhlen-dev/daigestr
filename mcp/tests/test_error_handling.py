"""
Tests für T-DAI-024: Error Handling Fixes.

FIX 1: Global Exception Handler in api_rest.py → 500 statt Worker-Crash
FIX 3: MAX_DESCRIBE_IMAGES Limit → nur N Bilder beschrieben, meta.images_truncated=True
FIX 4: CONVERT_TIMEOUT_SECONDS → saubere Error-Response bei Timeout

Alle Tests laufen ohne Docker-Container und ohne echte API-Calls.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conftest import load_server_module, run_async, PNG_100x100


# Einmal laden; alle Tests in diesem Modul teilen diese Instanz
_server = load_server_module(use_real_pil=False)
convert_auto = _server.convert_auto


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pdf_bytes() -> bytes:
    return b"%PDF-1.4 minimal"


def _base_convert_kwargs(**overrides) -> dict:
    defaults = dict(
        file_data=PNG_100x100,
        filename="test.png",
        source="test.png",
        source_type="base64",
        input_meta={},
        language="de",
    )
    defaults.update(overrides)
    return defaults


def _make_vision_response(content: str = "# OK") -> dict:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
    }


# ---------------------------------------------------------------------------
# Helpers: Zugriff auf echte Handler-Funktion (vor Mock-Decoration)
# ---------------------------------------------------------------------------

def _get_real_handler(mock_app, handler_name: str):
    """
    Extrahiert die echte Handler-Funktion aus den aufgezeichneten Mock-Calls.

    Da @app.exception_handler(X) ein Mock-Decorator ist, wird die echte Funktion
    als Argument an den Decorator-Mock übergeben und kann über call_args_list
    abgerufen werden.
    """
    import inspect
    decorator_mock = mock_app.exception_handler.return_value
    for call in decorator_mock.call_args_list:
        fn = call[0][0] if call[0] else None
        if fn and callable(fn) and getattr(fn, "__name__", "") == handler_name:
            return fn
    return None


# ---------------------------------------------------------------------------
# FIX 1: Global Exception Handler — Exception in convert_auto → 500
# ---------------------------------------------------------------------------


class TestGlobalExceptionHandler:
    """FIX 1: api_rest global_exception_handler fängt unerwartete Exceptions."""

    def test_exception_handler_registered_for_base_exception(self):
        """
        app.exception_handler wurde mit Exception (Basis-Klasse) als Argument aufgerufen.
        Das stellt sicher dass der Handler für ALLE unerwarteten Exceptions greift.
        """
        import api_rest as _api_rest

        # Prüfe ob exception_handler mit Exception aufgerufen wurde
        exception_handler_args = [
            call[0][0] if call[0] else None
            for call in _api_rest.app.exception_handler.call_args_list
        ]
        assert Exception in exception_handler_args, (
            f"app.exception_handler wurde nicht mit Exception aufgerufen. "
            f"Aufrufe: {exception_handler_args}"
        )

    def test_global_exception_handler_is_async(self):
        """
        global_exception_handler ist eine async-Funktion.
        """
        import api_rest as _api_rest
        import inspect

        handler = _get_real_handler(_api_rest.app, "global_exception_handler")
        assert handler is not None, "global_exception_handler nicht in exception_handler Aufrufen gefunden"
        assert inspect.iscoroutinefunction(handler)

    def test_global_exception_handler_returns_500_json(self):
        """
        global_exception_handler gibt JSONResponse mit status 500 und success=False zurück.
        """
        import api_rest as _api_rest
        import json
        from fastapi.responses import JSONResponse as _RealJSONResponse

        # Echte JSONResponse aus der fastapi.responses Mock holen — da fastapi gemockt
        # ist, nutzen wir starlette direkt
        from starlette.responses import JSONResponse as StarletteJSONResponse

        handler = _get_real_handler(_api_rest.app, "global_exception_handler")
        assert handler is not None, "Handler nicht gefunden"

        mock_request = MagicMock()
        mock_request.url = MagicMock()
        mock_request.url.__str__ = lambda self: "http://testserver/v1/convert"

        exc = RuntimeError("Simulated crash in convert_auto")

        # JSONResponse ist in api_rest aus fastapi.responses importiert — aber das ist gemockt.
        # Wir patchen JSONResponse direkt auf die echte Starlette-Implementierung.
        with patch.object(_api_rest, "JSONResponse", StarletteJSONResponse):
            result = run_async(handler(mock_request, exc))

        assert result.status_code == 500
        body = json.loads(result.body)
        assert body["success"] is False
        assert body["error_code"] == "INTERNAL_ERROR"
        assert "Simulated crash" in body["error"]

    def test_global_exception_handler_logs_error(self):
        """
        global_exception_handler loggt die Exception mit error + type + path.
        """
        import api_rest as _api_rest
        from starlette.responses import JSONResponse as StarletteJSONResponse

        handler = _get_real_handler(_api_rest.app, "global_exception_handler")
        assert handler is not None, "Handler nicht gefunden"

        mock_request = MagicMock()
        mock_request.url = MagicMock()
        mock_request.url.__str__ = lambda self: "http://testserver/v1/convert"

        exc = ValueError("Test error")
        mock_log = MagicMock()

        with patch.object(_api_rest, "log", mock_log), \
             patch.object(_api_rest, "JSONResponse", StarletteJSONResponse):
            run_async(handler(mock_request, exc))

        mock_log.error.assert_called_once()
        call_args = mock_log.error.call_args
        # Muss "unhandled_exception" als erstes Arg haben
        assert call_args[0][0] == "unhandled_exception"


# ---------------------------------------------------------------------------
# FIX 3: MAX_DESCRIBE_IMAGES — Truncation bei zu vielen Bildern
# ---------------------------------------------------------------------------


class TestMaxDescribeImages:
    """FIX 3: MAX_DESCRIBE_IMAGES limitiert die Anzahl beschriebener Bilder."""

    def _make_fake_images(self, n: int) -> list:
        """Erstellt n Fake-Bild-Einträge (wie extract_images_from_pdf zurückgibt)."""
        return [{"data": PNG_100x100, "mimetype": "image/png", "index": i} for i in range(n)]

    def _pdf_convert_kwargs(self, **overrides) -> dict:
        defaults = dict(
            file_data=_make_pdf_bytes(),
            filename="test.pdf",
            source="test.pdf",
            source_type="file",
            input_meta={},
            language="de",
            describe_images=True,
        )
        defaults.update(overrides)
        return defaults

    def _run_with_pdf_patches(self, max_images: int, fake_images: list) -> object:
        """Führt convert_auto für ein PDF mit gepatchten Extraktions-Funktionen aus."""
        markitdown_result = {
            "success": True,
            "markdown": "# Doc\n\nSome text.",
            "title": None,
        }

        async def _fake_describe(images, language="de"):
            return [{"index": img["index"], "description": f"Image {img['index']}"} for img in images]

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "MAX_DESCRIBE_IMAGES", max_images), \
             patch.object(_server, "convert_with_markitdown", return_value=markitdown_result), \
             patch.object(_server, "is_scanned_pdf", return_value=False), \
             patch.object(_server, "detect_mimetype_from_bytes", return_value=None), \
             patch.object(_server, "extract_images_from_pdf", return_value=fake_images), \
             patch.object(_server, "describe_embedded_images", new=AsyncMock(side_effect=_fake_describe)), \
             patch.object(_server, "insert_image_descriptions", side_effect=lambda md, descs: md):
            return run_async(convert_auto(**self._pdf_convert_kwargs()))

    def test_more_than_max_images_truncated(self):
        """
        Wenn describe_images=True und das Dokument mehr als MAX_DESCRIBE_IMAGES Bilder hat,
        werden nur die ersten N beschrieben und meta.images_truncated=True gesetzt.
        """
        max_images = 3
        total_images = 10
        fake_images = self._make_fake_images(total_images)

        result = self._run_with_pdf_patches(max_images, fake_images)

        assert result.success is True
        assert getattr(result.meta, "images_truncated", None) is True
        assert result.meta.images_described == max_images

    def test_fewer_than_max_images_not_truncated(self):
        """
        Wenn die Anzahl der Bilder unter MAX_DESCRIBE_IMAGES liegt,
        wird NICHT truncated und images_truncated ist nicht gesetzt.
        """
        max_images = 50
        total_images = 3
        fake_images = self._make_fake_images(total_images)

        result = self._run_with_pdf_patches(max_images, fake_images)

        assert result.success is True
        # images_truncated ist ein extra-Feld — wenn nicht gesetzt, ist getattr None/falsy
        assert not getattr(result.meta, "images_truncated", None)
        assert result.meta.images_described == total_images

    def test_truncation_hint_added_to_meta(self):
        """
        Bei Truncation wird ein Hinweis in meta.hints eingefügt.
        """
        max_images = 2
        total_images = 5
        fake_images = self._make_fake_images(total_images)

        result = self._run_with_pdf_patches(max_images, fake_images)

        assert result.success is True
        assert getattr(result.meta, "images_truncated", None) is True
        hints = result.meta.hints or []
        assert any("MAX_DESCRIBE_IMAGES" in h for h in hints), f"Expected truncation hint, got: {hints}"
        assert any(str(total_images) in h for h in hints), f"Expected total count in hint, got: {hints}"


# ---------------------------------------------------------------------------
# FIX 4: Timeout → saubere Error-Response
# ---------------------------------------------------------------------------


def _run_with_new_loop(coro):
    """Führt eine Coroutine in einem frischen Event-Loop aus (für Timeout-Tests)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestConvertTimeout:
    """FIX 4: CONVERT_TIMEOUT_SECONDS → TimeoutError ergibt saubere Error-Response."""

    def _fresh_convert_auto(self):
        """
        Lädt server frisch (eigene Instanz) und gibt (server, convert_auto) zurück.
        Damit sind Modul-State-Konflikte zwischen Test-Modulen ausgeschlossen.
        """
        fresh_server = load_server_module()
        return fresh_server, fresh_server.convert_auto

    def test_timeout_returns_error_response_not_exception(self):
        """
        Wenn asyncio.wait_for einen TimeoutError auslöst,
        gibt convert_auto eine saubere Error-Response zurück (kein raise).

        Nutzt frisch geladenes server-Modul um Modul-State-Konflikte zu vermeiden.
        """
        fresh_server, fresh_convert_auto = self._fresh_convert_auto()
        import sys
        fresh_routing = sys.modules.get("routing")
        assert fresh_routing is not None

        async def _slow(*args, **kwargs):
            await asyncio.sleep(60)

        timeout_val = 0.1
        with patch.object(fresh_server, "CONVERT_TIMEOUT_SECONDS", timeout_val), \
             patch.object(fresh_routing, "CONVERT_TIMEOUT_SECONDS", timeout_val), \
             patch.object(fresh_server, "_convert_auto_impl", new=_slow, create=True), \
             patch.object(fresh_routing, "_convert_auto_impl", new=_slow):
            result = _run_with_new_loop(fresh_convert_auto(**_base_convert_kwargs()))

        assert result.success is False
        assert result.error is not None
        error_msg = result.error.message if hasattr(result.error, "message") else str(result.error)
        assert "Timeout" in error_msg or "timeout" in error_msg.lower(), f"Expected timeout msg, got: {error_msg}"
        assert result.error.code is not None

    def test_timeout_includes_timeout_seconds_in_response(self):
        """
        Die Timeout-Error-Response enthält die konfigurierte Timeout-Dauer.
        """
        fresh_server, fresh_convert_auto = self._fresh_convert_auto()
        import sys
        fresh_routing = sys.modules.get("routing")
        assert fresh_routing is not None

        async def _slow(*args, **kwargs):
            await asyncio.sleep(60)

        timeout_val = 0.1
        with patch.object(fresh_server, "CONVERT_TIMEOUT_SECONDS", timeout_val), \
             patch.object(fresh_routing, "CONVERT_TIMEOUT_SECONDS", timeout_val), \
             patch.object(fresh_server, "_convert_auto_impl", new=_slow, create=True), \
             patch.object(fresh_routing, "_convert_auto_impl", new=_slow):
            result = _run_with_new_loop(fresh_convert_auto(**_base_convert_kwargs()))

        assert result.success is False
        error_msg = result.error.message if hasattr(result.error, "message") else str(result.error)
        assert str(timeout_val) in error_msg

    def test_no_timeout_on_fast_conversion(self):
        """
        Bei einer schnellen Konvertierung wird KEIN Timeout ausgelöst.
        """
        fresh_server, fresh_convert_auto = self._fresh_convert_auto()
        import sys
        fresh_routing = sys.modules.get("routing")
        assert fresh_routing is not None
        from models import create_success_response

        async def _fast(*args, **kwargs):
            return create_success_response("# OK", meta={})

        with patch.object(fresh_server, "CONVERT_TIMEOUT_SECONDS", 30), \
             patch.object(fresh_routing, "CONVERT_TIMEOUT_SECONDS", 30), \
             patch.object(fresh_server, "_convert_auto_impl", new=_fast, create=True), \
             patch.object(fresh_routing, "_convert_auto_impl", new=_fast):
            result = _run_with_new_loop(fresh_convert_auto(**_base_convert_kwargs()))

        assert result.success is True
        assert result.markdown == "# OK"
