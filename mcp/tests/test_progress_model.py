import progress_tracking


def test_build_progress_payload_materializes_canonical_fields():
    payload = progress_tracking.build_progress_payload(
        status="processing",
        current_stage="extract",
        message="extracting structured data",
        percent=80,
        request_id="req-1",
        job_id="job-1",
        attempt_number=2,
        attempt_count=2,
        attempt_mode="full",
        page_current=52,
        page_total=52,
        upstream_attempt=3,
        metadata={"filename": "scan.pdf"},
    )

    assert payload == {
        "status": "processing",
        "current_stage": "extract",
        "message": "extracting structured data",
        "percent": 80,
        "request_id": "req-1",
        "job_id": "job-1",
        "attempt_number": 2,
        "attempt_count": 2,
        "attempt_mode": "full",
        "page_current": 52,
        "page_total": 52,
        "upstream_attempt": 3,
        "metadata": {"filename": "scan.pdf"},
    }


def test_build_progress_payload_keeps_nulls_for_known_fields():
    payload = progress_tracking.build_progress_payload(current_stage="queued")

    assert payload["status"] == "processing"
    assert payload["current_stage"] == "queued"
    assert payload["message"] is None
    assert payload["percent"] is None
    assert payload["attempt_number"] is None
    assert payload["page_current"] is None
    assert payload["metadata"] == {}


def test_normalize_progress_payload_maps_legacy_keys():
    payload = progress_tracking.normalize_progress_payload(
        {"step": "ocr", "detail": "page 1/5", "percent": 20}
    )

    assert payload == {
        "status": "processing",
        "current_stage": "ocr",
        "message": "page 1/5",
        "percent": 20,
        "request_id": None,
        "job_id": None,
        "attempt_number": None,
        "attempt_count": None,
        "attempt_mode": None,
        "page_current": None,
        "page_total": None,
        "upstream_attempt": None,
        "metadata": {},
    }
