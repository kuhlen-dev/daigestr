"""
E2E Baseline Tests for PostgreSQL Migration — T-DAI-030

Documents the IST-Zustand of the running Daigestr container.
Run against http://127.0.0.1:18006 (or DAIGESTR_URL env var).
Re-run after migration to verify parity.

Usage:
    cd mcp && python3 -m pytest tests/test_e2e_postgres_migration.py -v --tb=short
"""

import base64
import os
import time

import httpx
import pytest

BASE_URL = os.getenv("DAIGESTR_URL", "http://127.0.0.1:18006")
TIMEOUT = 120  # seconds per test
LLM_TIMEOUT = 30  # seconds for tests that call Mistral

# --- Small test payloads (base64-encoded) ---

TXT_B64 = base64.b64encode(b"Hello World. This is a baseline test document.").decode()
TXT_FILENAME = "baseline_test.txt"

CSV_B64 = base64.b64encode(
    b"Name,Age,City\nAlice,30,Berlin\nBob,25,Munich\nCarla,35,Hamburg\n"
).decode()
CSV_FILENAME = "test_data.csv"

INVOICE_TXT_B64 = base64.b64encode(
    b"Rechnung Nr. 2024-001\n"
    b"Datum: 01.03.2024\n"
    b"Rechnungssteller: Muster GmbH, Musterstr. 1, 10115 Berlin\n"
    b"Rechnungsempfaenger: Test AG, Teststr. 2, 80333 Muenchen\n"
    b"Position 1: Beratungsleistung, 10 Stunden x 100 EUR = 1000 EUR\n"
    b"Nettobetrag: 1000 EUR\n"
    b"MwSt. 19%: 190 EUR\n"
    b"Gesamtbetrag: 1190 EUR\n"
    b"IBAN: DE89 3704 0044 0532 0130 00\n"
    b"Bitte ueberweisen Sie den Betrag bis zum 31.03.2024.\n"
).decode()
INVOICE_FILENAME = "rechnung_test.txt"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _health() -> dict:
    """Return parsed health response (cached within a test run)."""
    r = httpx.get(f"{BASE_URL}/v1/health", timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _mistral_available() -> bool:
    try:
        h = _health()
        return bool(h.get("meta", {}).get("mistral_api_configured", False))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 1. Health
# ---------------------------------------------------------------------------


def test_01_health_ok():
    """GET /v1/health → status ok"""
    r = httpx.get(f"{BASE_URL}/v1/health", timeout=TIMEOUT)
    assert r.status_code == 200
    data = r.json()
    assert data.get("status") == "ok"


# ---------------------------------------------------------------------------
# 2. Templates — at least 100
# ---------------------------------------------------------------------------


def test_02_templates_min_100():
    """GET /v1/templates → at least 100 templates"""
    r = httpx.get(f"{BASE_URL}/v1/templates", timeout=TIMEOUT)
    assert r.status_code == 200
    data = r.json()
    # Response is {"templates": [...]}
    templates = data.get("templates", data)
    assert len(templates) >= 100, f"Expected >= 100 templates, got {len(templates)}"


# ---------------------------------------------------------------------------
# 3. Template search
# ---------------------------------------------------------------------------


def test_03_template_search_invoice():
    """GET /v1/templates/search?q=invoice → at least 1 hit"""
    r = httpx.get(f"{BASE_URL}/v1/templates/search", params={"q": "invoice"}, timeout=TIMEOUT)
    assert r.status_code == 200
    data = r.json()
    templates = data.get("templates", data)
    assert len(templates) >= 1, "Expected at least 1 template matching 'invoice'"


# ---------------------------------------------------------------------------
# 4. Template categories
# ---------------------------------------------------------------------------


def test_04_template_categories_not_empty():
    """GET /v1/templates/categories → not empty"""
    r = httpx.get(f"{BASE_URL}/v1/templates/categories", timeout=TIMEOUT)
    assert r.status_code == 200
    data = r.json()
    categories = data.get("categories", data)
    assert len(categories) > 0, "Expected at least one template category"


# ---------------------------------------------------------------------------
# 5. Convert TXT via base64
# ---------------------------------------------------------------------------


def test_05_convert_txt_base64():
    """POST /v1/convert with base64 TXT → success:true, markdown not empty"""
    r = httpx.post(
        f"{BASE_URL}/v1/convert",
        json={"base64": TXT_B64, "filename": TXT_FILENAME},
        timeout=TIMEOUT,
    )
    assert r.status_code == 200
    data = r.json()
    assert data.get("success") is True
    assert data.get("markdown"), "markdown should not be empty"


# ---------------------------------------------------------------------------
# 6. CSV → table (pipe character)
# ---------------------------------------------------------------------------


def test_06_csv_to_table():
    """POST /v1/convert with CSV → markdown contains |"""
    r = httpx.post(
        f"{BASE_URL}/v1/convert",
        json={"base64": CSV_B64, "filename": CSV_FILENAME},
        timeout=TIMEOUT,
    )
    assert r.status_code == 200
    data = r.json()
    assert data.get("success") is True
    assert "|" in (data.get("markdown") or ""), "CSV markdown should contain table pipes"


# ---------------------------------------------------------------------------
# 7. Classify
# ---------------------------------------------------------------------------


def test_07_classify():
    """POST /v1/convert with classify:true → meta.document_type set"""
    if not _mistral_available():
        pytest.skip("Mistral API not configured")
    r = httpx.post(
        f"{BASE_URL}/v1/convert",
        json={"base64": INVOICE_TXT_B64, "filename": INVOICE_FILENAME, "classify": True},
        timeout=LLM_TIMEOUT,
    )
    assert r.status_code == 200
    data = r.json()
    assert data.get("success") is True
    assert data.get("meta", {}).get("document_type"), "meta.document_type should be set"


# ---------------------------------------------------------------------------
# 8. Chunk
# ---------------------------------------------------------------------------


def test_08_chunk():
    """POST /v1/convert with chunk:true → chunks not empty"""
    r = httpx.post(
        f"{BASE_URL}/v1/convert",
        json={"base64": TXT_B64, "filename": TXT_FILENAME, "chunk": True},
        timeout=TIMEOUT,
    )
    assert r.status_code == 200
    data = r.json()
    assert data.get("success") is True
    chunks = data.get("chunks")
    assert chunks is not None and len(chunks) > 0, "chunks should not be empty"


# ---------------------------------------------------------------------------
# 9. Quality score
# ---------------------------------------------------------------------------


def test_09_quality_score():
    """POST /v1/convert → meta.quality_score between 0 and 1"""
    r = httpx.post(
        f"{BASE_URL}/v1/convert",
        json={"base64": TXT_B64, "filename": TXT_FILENAME},
        timeout=TIMEOUT,
    )
    assert r.status_code == 200
    data = r.json()
    assert data.get("success") is True
    score = data.get("meta", {}).get("quality_score")
    assert score is not None, "meta.quality_score should be present"
    assert 0.0 <= float(score) <= 1.0, f"quality_score {score} out of range [0, 1]"


# ---------------------------------------------------------------------------
# 10. mode:full → chunks + classify
# ---------------------------------------------------------------------------


def test_10_mode_full():
    """POST /v1/convert mode:full → chunks + classify set"""
    if not _mistral_available():
        pytest.skip("Mistral API not configured")
    r = httpx.post(
        f"{BASE_URL}/v1/convert",
        json={"base64": INVOICE_TXT_B64, "filename": INVOICE_FILENAME, "mode": "full"},
        timeout=LLM_TIMEOUT,
    )
    assert r.status_code == 200
    data = r.json()
    assert data.get("success") is True
    assert data.get("chunks") is not None, "mode:full should produce chunks"
    assert data.get("meta", {}).get("document_type"), "mode:full should classify"


# ---------------------------------------------------------------------------
# 11. output_format:html
# ---------------------------------------------------------------------------


def test_11_output_format_html():
    """POST /v1/convert output_format:html → html field with <html"""
    r = httpx.post(
        f"{BASE_URL}/v1/convert",
        json={"base64": TXT_B64, "filename": TXT_FILENAME, "output_format": "html"},
        timeout=TIMEOUT,
    )
    assert r.status_code == 200
    data = r.json()
    assert data.get("success") is True
    html = data.get("html") or ""
    assert "<html" in html.lower(), "html field should contain <html tag"


# ---------------------------------------------------------------------------
# 12. output_format:text → no markdown headers
# ---------------------------------------------------------------------------


def test_12_output_format_text():
    """POST /v1/convert output_format:text → markdown field without leading #"""
    r = httpx.post(
        f"{BASE_URL}/v1/convert",
        json={"base64": TXT_B64, "filename": TXT_FILENAME, "output_format": "text"},
        timeout=TIMEOUT,
    )
    assert r.status_code == 200
    data = r.json()
    assert data.get("success") is True
    md = data.get("markdown") or ""
    # text output should not contain markdown heading syntax
    lines_with_heading = [ln for ln in md.splitlines() if ln.startswith("#")]
    assert not lines_with_heading, f"output_format:text should have no # headers, got: {lines_with_heading[:3]}"


# ---------------------------------------------------------------------------
# 13. Cache hit
# ---------------------------------------------------------------------------


def test_13_cache_hit():
    """Same request twice → 2nd response has meta.cached:true"""
    payload = {"base64": TXT_B64, "filename": TXT_FILENAME, "no_cache": False}
    # First request — prime the cache
    r1 = httpx.post(f"{BASE_URL}/v1/convert", json=payload, timeout=TIMEOUT)
    assert r1.status_code == 200
    assert r1.json().get("success") is True

    # Second request — should be cached
    r2 = httpx.post(f"{BASE_URL}/v1/convert", json=payload, timeout=TIMEOUT)
    assert r2.status_code == 200
    data2 = r2.json()
    assert data2.get("success") is True
    assert data2.get("meta", {}).get("cached") is True, "2nd identical request should be cached"


# ---------------------------------------------------------------------------
# 14. Cache clear
# ---------------------------------------------------------------------------


def test_14_cache_clear():
    """DELETE /v1/cache → cleared:true (or similar success indicator)"""
    r = httpx.delete(f"{BASE_URL}/v1/cache", timeout=TIMEOUT)
    assert r.status_code == 200
    data = r.json()
    # Accept either cleared:true or success:true
    assert data.get("cleared") is True or data.get("success") is True, (
        f"Cache clear response unexpected: {data}"
    )


# ---------------------------------------------------------------------------
# 15. no_cache flag
# ---------------------------------------------------------------------------


def test_15_no_cache():
    """POST /v1/convert no_cache:true → meta.cached not true"""
    # Prime cache first
    prime = httpx.post(
        f"{BASE_URL}/v1/convert",
        json={"base64": TXT_B64, "filename": TXT_FILENAME},
        timeout=TIMEOUT,
    )
    assert prime.status_code == 200

    # Now request with no_cache
    r = httpx.post(
        f"{BASE_URL}/v1/convert",
        json={"base64": TXT_B64, "filename": TXT_FILENAME, "no_cache": True},
        timeout=TIMEOUT,
    )
    assert r.status_code == 200
    data = r.json()
    assert data.get("success") is True
    assert data.get("meta", {}).get("cached") is not True, "no_cache:true should bypass cache"


# ---------------------------------------------------------------------------
# 16. Async job
# ---------------------------------------------------------------------------


def test_16_async_job():
    """POST /v1/convert/async → job_id returned; GET /v1/jobs/{id} → status field"""
    r = httpx.post(
        f"{BASE_URL}/v1/convert/async",
        json={"base64": TXT_B64, "filename": TXT_FILENAME},
        timeout=TIMEOUT,
    )
    assert r.status_code == 200
    data = r.json()
    job_id = data.get("job_id")
    assert job_id, "Expected job_id in async response"

    # Poll status
    status_r = httpx.get(f"{BASE_URL}/v1/jobs/{job_id}", timeout=TIMEOUT)
    assert status_r.status_code == 200
    status_data = status_r.json()
    assert "status" in status_data, "Job status response should have 'status' field"
    assert status_data["status"] in ("queued", "running", "completed", "failed"), (
        f"Unexpected job status: {status_data['status']}"
    )


# ---------------------------------------------------------------------------
# 17. Jobs list
# ---------------------------------------------------------------------------


def test_17_jobs_list():
    """GET /v1/jobs → jobs array"""
    r = httpx.get(f"{BASE_URL}/v1/jobs", timeout=TIMEOUT)
    assert r.status_code == 200
    data = r.json()
    assert "jobs" in data, "Response should have 'jobs' key"
    assert isinstance(data["jobs"], list), "jobs should be a list"


# ---------------------------------------------------------------------------
# 18. Webhook — fail silent
# ---------------------------------------------------------------------------


def test_18_webhook_fail_silent():
    """POST /v1/convert with unreachable webhook_url → success:true (webhook errors are silent)"""
    r = httpx.post(
        f"{BASE_URL}/v1/convert",
        json={
            "base64": TXT_B64,
            "filename": TXT_FILENAME,
            "webhook_url": "http://localhost:9999/nonexistent",
        },
        timeout=TIMEOUT,
    )
    assert r.status_code == 200
    data = r.json()
    assert data.get("success") is True, "Webhook delivery failure should not affect convert success"


# ---------------------------------------------------------------------------
# 19. Tips — optional_features coverage
# ---------------------------------------------------------------------------


def test_19_tips_optional_features():
    """GET /v1/tips → optional_features contains mode, output_format, pages, no_cache"""
    r = httpx.get(f"{BASE_URL}/v1/tips", timeout=TIMEOUT)
    assert r.status_code == 200
    data = r.json()
    features = data.get("optional_features", {})
    required_keys = {"mode", "output_format", "pages", "no_cache"}
    missing = required_keys - set(features.keys())
    assert not missing, f"optional_features missing keys: {missing}"


# ---------------------------------------------------------------------------
# 20. Formats — pdf present
# ---------------------------------------------------------------------------


def test_20_formats_pdf():
    """GET /v1/formats → pdf is listed"""
    r = httpx.get(f"{BASE_URL}/v1/formats", timeout=TIMEOUT)
    assert r.status_code == 200
    data = r.json()
    # Flatten all format lists and check for .pdf or pdf
    all_formats = []
    for v in data.values():
        if isinstance(v, list):
            all_formats.extend(v)
    pdf_present = any("pdf" in fmt for fmt in all_formats)
    assert pdf_present, f"Expected 'pdf' in formats, got: {all_formats[:20]}"


# ---------------------------------------------------------------------------
# 21. Error on nonexistent path
# ---------------------------------------------------------------------------


def test_21_error_nonexistent_path():
    """POST /v1/convert with path:/nonexistent → success:false"""
    r = httpx.post(
        f"{BASE_URL}/v1/convert",
        json={"path": "/nonexistent/baseline_test_file_xyz.txt"},
        timeout=TIMEOUT,
    )
    assert r.status_code == 200
    data = r.json()
    assert data.get("success") is False, "Non-existent path should return success:false"
    assert data.get("error") is not None, "Error details should be present"


# ---------------------------------------------------------------------------
# 22. prepare-batch
# ---------------------------------------------------------------------------


def test_22_prepare_batch():
    """POST /v1/prepare-batch with base64 TXT → batch_jobs array (can be empty for text)"""
    r = httpx.post(
        f"{BASE_URL}/v1/prepare-batch",
        json={"base64": TXT_B64, "filename": TXT_FILENAME},
        timeout=TIMEOUT,
    )
    assert r.status_code == 200
    data = r.json()
    assert "batch_jobs" in data, "Response should have 'batch_jobs' key"
    assert isinstance(data["batch_jobs"], list), "batch_jobs should be a list"


# ---------------------------------------------------------------------------
# 23. Template extraction (invoice)
# ---------------------------------------------------------------------------


def test_23_template_invoice_extraction():
    """POST /v1/convert with template:invoice and invoice text → extracted not None"""
    if not _mistral_available():
        pytest.skip("Mistral API not configured")
    r = httpx.post(
        f"{BASE_URL}/v1/convert",
        json={
            "base64": INVOICE_TXT_B64,
            "filename": INVOICE_FILENAME,
            "template": "invoice",
        },
        timeout=LLM_TIMEOUT,
    )
    assert r.status_code == 200
    data = r.json()
    assert data.get("success") is True
    assert data.get("extracted") is not None, (
        "extracted should not be None when template:invoice is used"
    )
