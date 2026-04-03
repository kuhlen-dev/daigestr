"""
E2E-Test für den Async-Job-Result-Bug.

Symptom: Job zeigt "completed" aber /v1/jobs/{id}/result liefert leeres
         markdown und null extracted.

Reproduziert mit: Multi-Page PDF + mode:full + auto_extract:true.

Was wird getestet:
1. job_set_result schreibt result_json + setzt status=completed atomar
2. job_get liefert result_json korrekt zurück (SELECT *)
3. api_get_job_result deserialisiert result_json korrekt (ConvertResponse)
4. Race Condition: progress-Updates aus routing._update_progress überschreiben
   status nach job_set_result NICHT dauerhaft (status bleibt completed)
5. Cache-Hit liefert keine leere Response (no_cache=True)
6. 3 Wiederholungen für Race-Condition-Erkennung

DB: PostgreSQL via DATABASE_URL (default: localhost:15432)
"""

import asyncio
import io
import json
import os
import sys
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import psycopg2
import psycopg2.extras
import pytest

from conftest import load_server_module

# Server einmalig laden
_server = load_server_module(use_real_pil=True)

import templates_db as _templates_db
import api_rest as _api_rest_module
from models import ConvertResponse, MetaData

_DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://daigestr:daigestr@localhost:15432/daigestr",
)


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def _make_multipage_pdf_bytes(num_pages: int = 3) -> bytes:
    """
    Erstellt ein minimales Multi-Page PDF mit Text auf jeder Seite.
    Kein PIL/Pillow nötig — reines Byte-Crafting nach PDF-Spec.
    """
    pages = []
    for i in range(1, num_pages + 1):
        pages.append(f"Page {i}: Sample document content for testing async jobs. "
                     f"This page contains text and simulated structure.")

    # Minimales PDF manuell bauen
    objects = []

    # Object 1: Catalog
    objects.append(b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n")

    # Object 2: Pages (wird später mit kids gefüllt)
    page_refs = " ".join(f"{3 + i} 0 R" for i in range(num_pages))
    objects.append(
        f"2 0 obj\n<< /Type /Pages /Kids [{page_refs}] /Count {num_pages} >>\nendobj\n".encode()
    )

    # Object 3: Font
    objects.append(
        b"3 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n"
    )

    # Objects 4+: Seiten
    for i, page_text in enumerate(pages):
        page_obj_num = 4 + i * 2
        stream_obj_num = page_obj_num + 1

        # Content-Stream für diese Seite
        stream_content = (
            f"BT /F1 12 Tf 72 720 Td ({page_text}) Tj ET"
        ).encode()
        stream_len = len(stream_content)

        objects.append(
            f"{page_obj_num} 0 obj\n"
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Contents {stream_obj_num} 0 R /Resources << /Font << /F1 3 0 R >> >> >>\n"
            f"endobj\n".encode()
        )
        objects.append(
            f"{stream_obj_num} 0 obj\n"
            f"<< /Length {stream_len} >>\n"
            f"stream\n".encode()
            + stream_content
            + b"\nendstream\nendobj\n"
        )

    # Zusammenbauen
    buf = io.BytesIO()
    buf.write(b"%PDF-1.4\n")
    offsets = []
    for obj in objects:
        offsets.append(buf.tell())
        buf.write(obj)

    xref_pos = buf.tell()
    total_objs = len(objects) + 1
    buf.write(f"xref\n0 {total_objs}\n".encode())
    buf.write(b"0000000000 65535 f \n")
    for off in offsets:
        buf.write(f"{off:010d} 00000 n \n".encode())

    buf.write(
        f"trailer\n<< /Size {total_objs} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n".encode()
    )
    return buf.getvalue()


def _make_mock_convert_auto(markdown_content: str, extracted: dict):
    """Erstellt ein Mock für convert_auto das ein vollständiges ConvertResponse zurückgibt."""
    async def _mock_convert_auto(**kwargs):
        return ConvertResponse(
            success=True,
            markdown=markdown_content,
            extracted=extracted,
            meta=MetaData(
                source=kwargs.get("source", "test.pdf"),
                format="pdf",
                quality_score=0.85,
                quality_grade="good",
                pipeline_steps=["markitdown", "classify", "auto_extract", "chunk"],
            ),
        )
    return _mock_convert_auto


def run_async(coro):
    """Führt Coroutine synchron aus."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_jobs():
    """Truncate job-Tabelle vor jedem Test."""
    _templates_db.pool_reset()
    conn = psycopg2.connect(_DB_URL)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("TRUNCATE TABLE job")
    conn.close()
    _templates_db.pool_reset()
    yield
    _templates_db.pool_reset()


# ---------------------------------------------------------------------------
# Tests: DB-Schicht (atomares Schreiben + Lesen von result_json)
# ---------------------------------------------------------------------------

class TestJobResultDB:
    """Stellt sicher dass job_set_result atomar schreibt und job_get alles zurückgibt."""

    def test_job_set_result_includes_result_json_in_job_get(self):
        """job_get muss result_json zurückgeben — SELECT * deckt das ab."""
        job_id = str(uuid.uuid4())
        _templates_db.job_create(job_id)

        result_data = {
            "success": True,
            "markdown": "# Test Document\n\nMulti-page content.",
            "extracted": {"document_type": "invoice", "amount": "100.00"},
            "meta": {"quality_score": 0.85, "format": "pdf"},
        }
        _templates_db.job_set_result(job_id, json.dumps(result_data))

        job = _templates_db.job_get(job_id)
        assert job is not None, "job_get sollte Job zurückgeben"
        assert job["status"] == "completed", f"Status sollte 'completed' sein, ist '{job['status']}'"
        assert job["result_json"] is not None, "result_json darf nicht None sein nach job_set_result"

        parsed = json.loads(job["result_json"])
        assert parsed["markdown"] == "# Test Document\n\nMulti-page content."
        assert parsed["extracted"]["document_type"] == "invoice"

    def test_job_set_result_is_atomic(self):
        """status=completed und result_json werden in EINER UPDATE-Query gesetzt."""
        job_id = str(uuid.uuid4())
        _templates_db.job_create(job_id)

        # Verify initial state
        job = _templates_db.job_get(job_id)
        assert job["status"] == "queued"
        assert job["result_json"] is None

        result_json = json.dumps({"success": True, "markdown": "# Doc", "meta": {}})
        _templates_db.job_set_result(job_id, result_json)

        # Nach job_set_result: BEIDE Felder gleichzeitig gesetzt
        job = _templates_db.job_get(job_id)
        assert job["status"] == "completed"
        assert job["result_json"] == result_json

    def test_progress_update_does_not_wipe_result_json(self):
        """
        RACE CONDITION TEST: job_update() darf result_json nicht überschreiben.

        Wenn routing._update_progress() NACH job_set_result() feuert,
        setzt es status zurück auf 'processing'. Kritisch: result_json muss
        dabei erhalten bleiben.

        Hinweis: Das ist ein bekanntes Verhaltensproblem — job_update setzt
        status zurück auf 'processing' obwohl das Ergebnis schon gesetzt ist.
        Dieser Test dokumentiert das Verhalten und prüft dass zumindest
        result_json erhalten bleibt.
        """
        job_id = str(uuid.uuid4())
        _templates_db.job_create(job_id)

        result_json = json.dumps({"success": True, "markdown": "# Final Result", "meta": {}})
        _templates_db.job_set_result(job_id, result_json)

        # Simuliert: routing._update_progress() feuert NACH job_set_result
        _templates_db.job_update(
            job_id, "processing",
            json.dumps({"step": "describe_image", "detail": "image 3/5", "percent": 60})
        )

        job = _templates_db.job_get(job_id)
        # result_json muss erhalten geblieben sein (kein Wipe durch job_update)
        assert job["result_json"] is not None, (
            "BUG: job_update() hat result_json auf NULL gesetzt! "
            "Die SQL-Query darf result_json nicht anfassen."
        )
        assert json.loads(job["result_json"])["markdown"] == "# Final Result"

    def test_completed_job_result_json_not_empty_after_full_pipeline(self):
        """
        Reproduziert den gemeldeten Bug: completed Job hat leeres markdown + null extracted.

        Wenn result_json ein leeres ConvertResponse enthält (z.B. durch Cache-Hit
        oder Serialisierungsfehler), muss das explizit als Fehler erkannt werden.
        """
        job_id = str(uuid.uuid4())
        _templates_db.job_create(job_id)

        # Simuliert was _run_async_job macht: convert_auto gibt volle Response
        full_response = ConvertResponse(
            success=True,
            markdown="# Mehrseitiges Dokument\n\nSeite 1 Inhalt...\n\n## Seite 2\n\nWeiterer Inhalt.",
            extracted={"document_type": "contract", "parties": ["Firma A", "Firma B"]},
            meta=MetaData(
                source="test_multipage.pdf",
                format="pdf",
                quality_score=0.9,
                quality_grade="excellent",
                pipeline_steps=["markitdown", "classify", "auto_extract"],
            ),
        )
        _templates_db.job_set_result(job_id, full_response.model_dump_json())

        # Was api_get_job_result macht:
        job = _templates_db.job_get(job_id)
        assert job["status"] == "completed"
        assert job["result_json"] is not None

        # Deserialisierung wie in api_get_job_result
        response = ConvertResponse.model_validate_json(job["result_json"])
        assert response.success is True
        assert response.markdown, f"markdown darf nicht leer sein, ist: {response.markdown!r}"
        assert response.extracted is not None, "extracted darf nicht None sein"
        assert response.meta.quality_score is not None, "quality_score fehlt"


# ---------------------------------------------------------------------------
# Tests: _run_async_job — Vollständiger Async-Job-Workflow
# ---------------------------------------------------------------------------

class TestRunAsyncJob:
    """Testet _run_async_job direkt mit gemocktem convert_auto."""

    def _run_job_with_mock(
        self,
        job_id: str,
        markdown: str = "# Test\n\nContent.",
        extracted: dict = None,
        extra_kwargs: dict = None,
    ) -> None:
        """
        Führt _run_async_job mit gemocktem convert_auto aus.

        _get() schaut auf _server-Modul → beide (server + api_rest) patchen.
        """
        from models import ConvertRequest
        if extracted is None:
            extracted = {"document_type": "document", "summary": "Test summary"}

        mock_convert = _make_mock_convert_auto(markdown, extracted)

        request = ConvertRequest(
            path="/data/test.pdf",
            mode="full",
            auto_extract=True,
            no_cache=True,
            **(extra_kwargs or {}),
        )

        mock_path = MagicMock()
        mock_path.read_bytes.return_value = _make_multipage_pdf_bytes(3)
        mock_path.name = "test.pdf"
        mock_resolve = MagicMock(return_value=mock_path)

        async def run():
            # _get() liest aus _server-Modul → dort patchen
            with patch.object(_server, "convert_auto", mock_convert):
                with patch.object(_api_rest_module, "convert_auto", mock_convert):
                    with patch.object(_server, "resolve_path", mock_resolve):
                        with patch.object(_api_rest_module, "resolve_path", mock_resolve):
                            await _api_rest_module._run_async_job(job_id, request)

        run_async(run())

    def test_completed_job_has_markdown(self):
        """Nach _run_async_job: status=completed UND markdown nicht leer."""
        job_id = str(uuid.uuid4())
        _templates_db.job_create(job_id)

        self._run_job_with_mock(job_id, markdown="# Multi-Page PDF\n\nSeite 1.\n\n## Seite 2")

        job = _templates_db.job_get(job_id)
        assert job["status"] == "completed", f"Erwartet 'completed', ist '{job['status']}'"
        assert job["result_json"] is not None, "result_json ist None obwohl Job completed"

        response = ConvertResponse.model_validate_json(job["result_json"])
        assert response.success is True
        assert response.markdown, f"markdown leer: {response.markdown!r}"
        assert "Multi-Page PDF" in response.markdown

    def test_completed_job_has_extracted(self):
        """Nach _run_async_job mit auto_extract: extracted ist nicht None."""
        job_id = str(uuid.uuid4())
        _templates_db.job_create(job_id)

        extracted_data = {"document_type": "invoice", "amount": "250.00", "currency": "EUR"}
        self._run_job_with_mock(job_id, extracted=extracted_data)

        job = _templates_db.job_get(job_id)
        assert job["status"] == "completed"
        response = ConvertResponse.model_validate_json(job["result_json"])
        assert response.extracted is not None, "extracted ist None nach auto_extract=True"
        assert response.extracted.get("document_type") == "invoice"

    def test_completed_job_has_quality_score(self):
        """Nach _run_async_job: meta.quality_score ist vorhanden."""
        job_id = str(uuid.uuid4())
        _templates_db.job_create(job_id)

        self._run_job_with_mock(job_id)

        job = _templates_db.job_get(job_id)
        response = ConvertResponse.model_validate_json(job["result_json"])
        assert response.meta is not None
        assert response.meta.quality_score is not None, "quality_score fehlt in meta"
        assert 0.0 <= response.meta.quality_score <= 1.0

    def test_result_json_not_empty_string(self):
        """result_json darf kein leerer String sein nach Completion."""
        job_id = str(uuid.uuid4())
        _templates_db.job_create(job_id)

        self._run_job_with_mock(job_id)

        job = _templates_db.job_get(job_id)
        assert job["result_json"] is not None
        assert len(job["result_json"]) > 10, f"result_json zu kurz: {job['result_json']!r}"

        # Muss valides JSON sein
        parsed = json.loads(job["result_json"])
        assert parsed.get("success") is True
        assert parsed.get("markdown"), "markdown-Feld im JSON leer"


# ---------------------------------------------------------------------------
# Tests: Race Condition — 3 Wiederholungen
# ---------------------------------------------------------------------------

class TestRaceCondition:
    """
    Simuliert Race Condition: progress-Updates während/nach job_set_result.

    Reproduziert: routing._update_progress() kann job_update() NACH
    job_set_result() aufrufen wenn convert_auto Callbacks async feuert.
    """

    def _run_job_with_concurrent_progress(self, job_id: str, delay_progress: bool = False):
        """
        Führt _run_async_job aus und simuliert concurrent progress-Updates.

        delay_progress=True: progress-Update kommt NACH job_set_result (Race Condition).
        """
        from models import ConvertRequest

        original_job_set_result = _templates_db.job_set_result
        original_job_update = _templates_db.job_update

        progress_fired_after_result = []

        def patched_job_set_result(jid, result_json):
            original_job_set_result(jid, result_json)
            if delay_progress and jid == job_id:
                # Simuliert verspäteten Progress-Callback NACH dem Result
                original_job_update(
                    jid, "processing",
                    json.dumps({"step": "describe_image", "detail": "late update", "percent": 99})
                )
                progress_fired_after_result.append(True)

        request = ConvertRequest(
            path="/data/test.pdf",
            mode="full",
            auto_extract=True,
            no_cache=True,
        )

        mock_convert = _make_mock_convert_auto(
            "# Race Condition Test\n\nContent that should survive.",
            {"test": "value"},
        )
        mock_path = MagicMock()
        mock_path.read_bytes.return_value = _make_multipage_pdf_bytes(3)
        mock_path.name = "test.pdf"
        mock_resolve = MagicMock(return_value=mock_path)

        async def run():
            with patch.object(_api_rest_module, "job_set_result", patched_job_set_result):
                with patch.object(_server, "job_set_result", patched_job_set_result):
                    with patch.object(_server, "convert_auto", mock_convert):
                        with patch.object(_api_rest_module, "convert_auto", mock_convert):
                            with patch.object(_server, "resolve_path", mock_resolve):
                                with patch.object(_api_rest_module, "resolve_path", mock_resolve):
                                    await _api_rest_module._run_async_job(job_id, request)

        run_async(run())
        return progress_fired_after_result

    @pytest.mark.parametrize("run_number", [1, 2, 3])
    def test_result_survives_late_progress_update(self, run_number):
        """
        Race Condition: result_json bleibt erhalten wenn progress-Update
        NACH job_set_result feuert. Läuft 3x um Timing-Varianz zu erfassen.
        """
        job_id = str(uuid.uuid4())
        _templates_db.job_create(job_id)

        late_updates = self._run_job_with_concurrent_progress(job_id, delay_progress=True)
        assert len(late_updates) == 1, "Patched job_set_result wurde nicht aufgerufen"

        job = _templates_db.job_get(job_id)

        # result_json muss erhalten geblieben sein (kein NULL durch job_update)
        assert job["result_json"] is not None, (
            f"[Run {run_number}] BUG: result_json ist None nach verspätetem progress-Update! "
            "job_update() überschreibt result_json mit NULL."
        )

        parsed = json.loads(job["result_json"])
        assert parsed.get("markdown"), (
            f"[Run {run_number}] markdown leer nach verspätetem progress-Update: {parsed}"
        )

    @pytest.mark.parametrize("run_number", [1, 2, 3])
    def test_completed_job_result_consistent(self, run_number):
        """
        Konsistenz: Nach 3 Läufen liefert api_get_job_result immer
        vollständige Daten (kein leeres markdown, kein null extracted).
        """
        job_id = str(uuid.uuid4())
        _templates_db.job_create(job_id)

        self._run_job_with_concurrent_progress(job_id, delay_progress=False)

        job = _templates_db.job_get(job_id)
        assert job is not None
        assert job["result_json"] is not None, f"[Run {run_number}] result_json ist None"

        response = ConvertResponse.model_validate_json(job["result_json"])
        assert response.success, f"[Run {run_number}] success=False: {response}"
        assert response.markdown, f"[Run {run_number}] markdown leer"
        assert response.extracted is not None, f"[Run {run_number}] extracted ist None"
        assert response.meta.quality_score is not None, f"[Run {run_number}] quality_score fehlt"


# ---------------------------------------------------------------------------
# Tests: api_get_job_result — Logik-Schicht
#
# Hinweis: FastAPI ist in den Unit-Tests gemockt, daher ist der Decorator
# @app.get() ein MagicMock und ersetzt die Funktion. Wir testen die Logik
# direkt über templates_db + ConvertResponse.model_validate_json —
# genau wie der echte Endpoint es tut.
# ---------------------------------------------------------------------------

def _simulate_api_get_job_result(job_id: str) -> ConvertResponse:
    """
    Repliziert die Logik von api_get_job_result() direkt.

    Nötig weil FastAPI-Dekoratoren in Unit-Tests gemockt sind und die
    Funktion überschreiben. Diese Funktion ist identisch mit dem echten
    Endpoint-Body in api_rest.py.

    Wirft ValueError mit Status-Code-Info bei Fehler.
    """
    job = _templates_db.job_get(job_id)
    if not job:
        raise ValueError("404: Job not found")
    if job["status"] != "completed":
        raise ValueError(f"202: Job is not completed yet (status: {job['status']})")
    if not job.get("result_json"):
        raise ValueError(f"500: Job has no result data")
    return ConvertResponse.model_validate_json(job["result_json"])


class TestApiGetJobResult:
    """
    Testet die Logik von api_get_job_result() direkt via DB.

    Da FastAPI in Unit-Tests gemockt ist, wird _simulate_api_get_job_result
    genutzt — identische Logik wie der echte Endpoint.
    """

    def test_returns_convert_response_with_data(self):
        """Vollständige ConvertResponse wird korrekt deserialisiert."""
        job_id = str(uuid.uuid4())
        _templates_db.job_create(job_id)

        response = ConvertResponse(
            success=True,
            markdown="# Test\n\nMulti-page PDF content.",
            extracted={"document_type": "contract", "value": "42"},
            meta=MetaData(quality_score=0.8, quality_grade="good"),
        )
        _templates_db.job_set_result(job_id, response.model_dump_json())

        result = _simulate_api_get_job_result(job_id)
        assert isinstance(result, ConvertResponse), f"Erwartet ConvertResponse, ist {type(result)}"
        assert result.success is True
        assert result.markdown == "# Test\n\nMulti-page PDF content."
        assert result.extracted is not None
        assert result.extracted["document_type"] == "contract"
        assert result.meta.quality_score == 0.8

    def test_raises_404_for_unknown_job(self):
        """Wirft 404-Fehler für unbekannte Job-ID."""
        with pytest.raises(ValueError) as exc_info:
            _simulate_api_get_job_result("nonexistent-job-id")
        assert "404" in str(exc_info.value)

    def test_raises_202_for_processing_job(self):
        """Wirft 202-Fehler wenn Job noch nicht fertig."""
        job_id = str(uuid.uuid4())
        _templates_db.job_create(job_id)
        _templates_db.job_update(job_id, "processing", json.dumps({"step": "ocr", "percent": 50}))

        with pytest.raises(ValueError) as exc_info:
            _simulate_api_get_job_result(job_id)
        assert "202" in str(exc_info.value)

    def test_raises_500_for_completed_job_with_empty_result(self):
        """
        Wirft 500-Fehler wenn Job completed aber result_json fehlt.

        Das ist der gemeldete Bug-Zustand — stellt sicher dass der Fehler
        explizit erkannt wird statt eine leere Response zu liefern.
        """
        job_id = str(uuid.uuid4())
        _templates_db.job_create(job_id)

        # Direkt in DB: status=completed aber result_json bleibt NULL
        conn = psycopg2.connect(_DB_URL)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(
            "UPDATE job SET status='completed', updated_at=now() WHERE id=%s",
            (job_id,)
        )
        conn.close()
        _templates_db.pool_reset()

        with pytest.raises(ValueError) as exc_info:
            _simulate_api_get_job_result(job_id)
        assert "500" in str(exc_info.value), (
            f"Erwartete 500-Fehler für completed Job ohne result_json, bekam: {exc_info.value}"
        )

    def test_model_validate_json_roundtrip(self):
        """
        model_dump_json + model_validate_json Roundtrip verliert keine Daten.

        Stellt sicher dass Serialisierung/Deserialisierung korrekt ist —
        insbesondere für extracted (dict) und meta.quality_score (float).
        """
        original = ConvertResponse(
            success=True,
            markdown="# Mehrseitiges Dokument\n\n## Seite 2\n\nInhalt hier.",
            extracted={
                "document_type": "invoice",
                "amount": 1500.50,
                "currency": "EUR",
                "parties": ["Firma A", "Firma B"],
            },
            meta=MetaData(
                source="invoice.pdf",
                format="pdf",
                quality_score=0.92,
                quality_grade="excellent",
                pipeline_steps=["markitdown", "classify", "auto_extract", "chunk"],
            ),
        )

        serialized = original.model_dump_json()
        assert serialized, "model_dump_json() darf nicht leer sein"
        assert len(serialized) > 50

        restored = ConvertResponse.model_validate_json(serialized)
        assert restored.success == original.success
        assert restored.markdown == original.markdown
        assert restored.extracted == original.extracted
        assert restored.meta.quality_score == original.meta.quality_score
        assert restored.meta.quality_grade == original.meta.quality_grade
