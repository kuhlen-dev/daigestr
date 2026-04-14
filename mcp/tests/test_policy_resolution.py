import os
import sys
import uuid

import pytest

from conftest import load_server_module, run_async


DATABASE_URL = "postgresql://daigestr:daigestr@localhost:15432/daigestr"
os.environ["DATABASE_URL"] = DATABASE_URL


@pytest.fixture(autouse=True)
def _force_test_database(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)


_server = load_server_module(use_real_pil=False, isolate_runtime_state=False)


def test_resolve_mode_policy_deep_forces_expected_flags():
    policy = _server._resolve_mode_policy(
        mode="deep",
        describe_images=False,
        describe_pages=False,
        accuracy="standard",
        ocr_correct=False,
        auto_extract=False,
        chunk=False,
    )

    assert policy["describe_pages"] is True
    assert policy["describe_images"] is True
    assert policy["accuracy"] == "high"
    assert policy["ocr_correct"] is True
    assert policy["auto_extract"] is True
    assert policy["chunk"] is True
    assert "describe_images" in policy["forced_features"]


def test_resolve_retry_policy_uses_env_defaults(monkeypatch):
    monkeypatch.setattr(_server, "QUALITY_RETRY_ENABLED", True)
    monkeypatch.setattr(_server, "QUALITY_RETRY_THRESHOLD", 0.71)
    monkeypatch.setattr(_server, "QUALITY_RETRY_MODE", "full")

    policy = _server._resolve_retry_policy(
        retry_on_low_quality=None,
        quality_retry_threshold=None,
        quality_retry_mode=None,
        mode="default",
        extraction_requested=True,
    )

    assert policy["enabled"] is True
    assert policy["threshold"] == 0.71
    assert policy["retry_mode"] == "full"
    assert policy["eligible"] is True


def test_resolve_normalization_policy_prefers_template_used():
    policy = _server._resolve_normalization_policy(
        compact=True,
        requested_template="invoice",
        meta={"template_used": "telecom_bill", "document_type": "invoice"},
        auto_extract=True,
        extract_schema=None,
    )

    assert policy["requested"] is True
    assert policy["apply_normalizer"] is True
    assert policy["compact"] is True
    assert policy["resolved_template"] == "telecom_bill"


def test_resolve_long_document_policy_uses_threshold(monkeypatch):
    monkeypatch.setattr(_server, "LONG_DOCUMENT_PAGE_THRESHOLD", 20)
    monkeypatch.setattr(_server, "PAGE_DESCRIBE_MAX_PAGES", 60)

    policy = _server._resolve_long_document_policy(
        filename="demo.pdf",
        source_type="file",
        describe_pages=True,
        page_count=21,
    )

    assert policy["threshold"] == 20
    assert policy["page_describe_max_pages"] == 60
    assert policy["is_long_document"] is True
    assert policy["describe_pages_requested"] is True


def test_convert_auto_persists_resolved_policy_context(monkeypatch):
    import routing
    from templates_db import pool_reset
    from execution_db import execution_get, init_execution_db
    from models import create_success_response

    pool_reset()
    init_execution_db()
    request_id = f"req-{uuid.uuid4()}"

    async def fake_impl(**kwargs):
        return create_success_response(
            "# ok",
            meta={
                "quality_score": 0.88,
                "template_used": "invoice",
                "template_version": 1,
                "pages_processed": 26,
            },
        )

    monkeypatch.setattr(routing, "_convert_auto_impl", fake_impl)
    server_module = sys.modules.get("server")
    if server_module is not None:
        monkeypatch.setitem(server_module.__dict__, "_convert_auto_impl", fake_impl)
        monkeypatch.setitem(server_module.__dict__, "QUALITY_RETRY_ENABLED", True)
        monkeypatch.setitem(server_module.__dict__, "QUALITY_RETRY_THRESHOLD", 0.75)
        monkeypatch.setitem(server_module.__dict__, "QUALITY_RETRY_MODE", "full")
        monkeypatch.setitem(server_module.__dict__, "LONG_DOCUMENT_PAGE_THRESHOLD", 25)
    monkeypatch.setattr(routing, "QUALITY_RETRY_ENABLED", True)
    monkeypatch.setattr(routing, "QUALITY_RETRY_THRESHOLD", 0.75)
    monkeypatch.setattr(routing, "QUALITY_RETRY_MODE", "full")
    monkeypatch.setattr(routing, "LONG_DOCUMENT_PAGE_THRESHOLD", 25)

    response = run_async(
        routing.convert_auto(
            file_data=b"hello",
            filename="hello.pdf",
            source="/tmp/hello.pdf",
            source_type="file",
            input_meta={"_request_id": request_id},
            auto_extract=True,
            no_cache=True,
        )
    )

    execution_row = execution_get(response.meta.execution_id)
    policy_context = execution_row["policy_context"]

    assert policy_context["mode_policy"]["mode"] == "default"
    assert policy_context["mode_policy"]["classify"] is True
    assert policy_context["retry_policy"]["enabled"] is True
    assert policy_context["retry_policy"]["threshold"] == 0.75
    assert policy_context["normalization_policy"]["resolved_template"] == "invoice"
    assert policy_context["long_document_policy"]["is_long_document"] is True
    assert policy_context["long_document_policy"]["page_count"] == 26
