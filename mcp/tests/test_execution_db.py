"""
Tests für execution_db.py (E16/W16.1/T16.1.1).

Requires a live PostgreSQL at DATABASE_URL (default: postgresql://daigestr:daigestr@localhost:15432/daigestr).
Run: DATABASE_URL=postgresql://daigestr:daigestr@localhost:15432/daigestr python3 -m pytest tests/test_execution_db.py -v --tb=short
"""

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

pytestmark = pytest.mark.skipif(
    "localhost" not in os.getenv("DATABASE_URL", ""),
    reason="Requires local PostgreSQL (DATABASE_URL must contain 'localhost')",
)


@pytest.fixture(scope="module", autouse=True)
def setup_db():
    from templates_db import init_templates_db, pool_reset
    from execution_db import init_execution_db

    pool_reset()
    init_templates_db()
    init_execution_db()
    yield
    pool_reset()


def test_execution_tables_exist():
    from templates_db import get_db_connection, _return_conn

    expected = {"execution", "execution_attempt", "execution_result", "execution_batch", "execution_batch_item", "execution_queue"}
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        tables = {r["tablename"] for r in cur.fetchall()}
    finally:
        _return_conn(conn)

    for table_name in expected:
        assert table_name in tables, f"Table missing: {table_name}"


def test_persistence_health_includes_execution_tables():
    from templates_db import check_persistence_health

    health = check_persistence_health()
    assert "execution" in health["required_tables_checked"]
    assert "execution_attempt" in health["required_tables_checked"]
    assert "execution_result" in health["required_tables_checked"]
    assert "execution_batch" in health["required_tables_checked"]
    assert "execution_batch_item" in health["required_tables_checked"]
    assert "execution_queue" in health["required_tables_checked"]
    assert "execution" in health["present_tables"]
    assert "execution_attempt" in health["present_tables"]
    assert "execution_result" in health["present_tables"]
    assert "execution_batch" in health["present_tables"]
    assert "execution_batch_item" in health["present_tables"]
    assert "execution_queue" in health["present_tables"]


@pytest.fixture
def execution_record():
    from execution_db import (
        execution_create,
        execution_get,
    )
    from templates_db import get_db_connection, _return_conn

    execution_id = f"exec-{uuid.uuid4()}"
    request_id = f"req-{uuid.uuid4()}"

    created = execution_create(
        execution_id=execution_id,
        request_id=request_id,
        idempotency_key=f"idem-{execution_id}",
        execution_kind="direct",
        source_type="file",
        source_ref="/data/test.pdf",
        document_identity={"path": "/data/test.pdf", "size": 123},
        policy_context={"default_classify": True},
        status="queued",
        current_stage="start",
    )
    yield created

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM execution_result WHERE execution_id = %s", (execution_id,))
        cur.execute("DELETE FROM execution_attempt WHERE execution_id = %s", (execution_id,))
        cur.execute("DELETE FROM execution WHERE id = %s", (execution_id,))
        conn.commit()
    finally:
        _return_conn(conn)

    assert execution_get(execution_id) is None


def test_execution_create_and_get(execution_record):
    from execution_db import execution_get, execution_get_by_request_id, execution_get_by_idempotency_key

    row = execution_get(execution_record["id"])
    assert row is not None
    assert row["request_id"] == execution_record["request_id"]
    assert row["execution_kind"] == "direct"
    assert row["status"] == "queued"
    assert row["current_stage"] == "start"
    assert row["document_identity"]["path"] == "/data/test.pdf"

    by_request = execution_get_by_request_id(execution_record["request_id"])
    assert by_request is not None
    assert by_request["id"] == execution_record["id"]

    by_idempotency = execution_get_by_idempotency_key(execution_record["idempotency_key"])
    assert by_idempotency is not None
    assert by_idempotency["id"] == execution_record["id"]


def test_execution_update(execution_record):
    from execution_db import execution_update

    updated = execution_update(
        execution_record["id"],
        status="processing",
        current_stage="ocr",
        warning_summary={"warnings": ["used_retry"]},
        started_at_now=True,
    )
    assert updated is not None
    assert updated["status"] == "processing"
    assert updated["current_stage"] == "ocr"
    assert updated["warning_summary"]["warnings"] == ["used_retry"]
    assert updated["started_at"] is not None


def test_execution_queue_enqueue_claim_and_complete(execution_record):
    from execution_db import (
        execution_queue_enqueue,
        execution_queue_claim_next,
        execution_queue_complete,
        execution_queue_list,
    )

    queue_name = f"queue-{uuid.uuid4()}"
    queued = execution_queue_enqueue(
        queue_id=f"queue-{uuid.uuid4()}",
        execution_id=execution_record["id"],
        job_id="job-queue-test",
        payload={"request": {"path": "/data/test.pdf"}},
        queue_name=queue_name,
    )
    assert queued["status"] == "queued"
    assert queued["job_id"] == "job-queue-test"

    claimed = execution_queue_claim_next(worker_id="worker-a", lease_seconds=30, queue_name=queue_name)
    assert claimed is not None
    assert claimed["id"] == queued["id"]
    assert claimed["status"] == "claimed"
    assert claimed["claimed_by"] == "worker-a"

    completed = execution_queue_complete(claimed["id"])
    assert completed is not None
    assert completed["status"] == "completed"

    rows = execution_queue_list(limit=10)
    assert any(row["id"] == queued["id"] for row in rows)


def test_execution_batch_create_and_item_linkage(execution_record):
    from execution_db import (
        execution_batch_create,
        execution_batch_get,
        execution_batch_get_by_idempotency_key,
        execution_batch_item_create,
        execution_batch_item_list,
        execution_update,
    )

    batch_id = f"batch-{uuid.uuid4()}"
    batch = execution_batch_create(
        batch_id=batch_id,
        batch_ref="family-import",
        idempotency_key=f"batch-idem-{uuid.uuid4()}",
        queue_name="default",
        status="queued",
        item_count=1,
        metadata={"source": "brix"},
    )
    assert batch["id"] == batch_id
    assert batch["item_count"] == 1

    by_id = execution_batch_get(batch_id)
    assert by_id is not None
    assert by_id["batch_ref"] == "family-import"

    by_idempotency = execution_batch_get_by_idempotency_key(batch["idempotency_key"])
    assert by_idempotency is not None
    assert by_idempotency["id"] == batch_id

    batch_item_id = f"item-{uuid.uuid4()}"
    execution_update(execution_record["id"], batch_id=batch_id, batch_item_id=batch_item_id)
    item = execution_batch_item_create(
        batch_item_id=batch_item_id,
        batch_id=batch_id,
        item_index=0,
        execution_id=execution_record["id"],
        request_id=execution_record["request_id"],
        source_type="file",
        source_ref="/data/test.pdf",
        filename="test.pdf",
        status="queued",
        item_key="item-key",
        metadata={"document_id": 123},
        document_identity={"filename": "test.pdf"},
        input_snapshot={"resolved_path": "/data/test.pdf"},
    )
    assert item["execution_id"] == execution_record["id"]
    assert item["metadata"]["document_id"] == 123

    items = execution_batch_item_list(batch_id)
    assert len(items) == 1
    assert items[0]["id"] == batch_item_id


def test_execution_attempt_upsert_and_list(execution_record):
    from execution_db import execution_attempt_upsert, execution_attempt_list

    attempt = execution_attempt_upsert(
        attempt_id=f"attempt-{uuid.uuid4()}",
        execution_id=execution_record["id"],
        attempt_number=1,
        attempt_mode="default",
        attempt_reason="initial",
        status="processing",
        started_at_now=True,
    )
    assert attempt["attempt_number"] == 1
    assert attempt["attempt_mode"] == "default"
    assert attempt["status"] == "processing"

    updated_attempt = execution_attempt_upsert(
        attempt_id=attempt["id"],
        execution_id=execution_record["id"],
        attempt_number=1,
        attempt_mode="full",
        attempt_reason="low_quality_retry",
        status="completed",
        quality_score=0.81,
        retry_trigger="low_quality",
        finished_at_now=True,
    )
    assert updated_attempt["attempt_mode"] == "full"
    assert updated_attempt["attempt_reason"] == "low_quality_retry"
    assert updated_attempt["status"] == "completed"
    assert updated_attempt["quality_score"] == pytest.approx(0.81)
    assert updated_attempt["retry_trigger"] == "low_quality"
    assert updated_attempt["finished_at"] is not None

    attempts = execution_attempt_list(execution_record["id"])
    assert len(attempts) == 1
    assert attempts[0]["id"] == attempt["id"]


def test_execution_result_upsert_and_fetch_final(execution_record):
    from execution_db import (
        execution_attempt_upsert,
        execution_result_upsert,
        execution_result_get_final,
        execution_result_list,
    )

    attempt = execution_attempt_upsert(
        attempt_id=f"attempt-{uuid.uuid4()}",
        execution_id=execution_record["id"],
        attempt_number=2,
        attempt_mode="full",
        attempt_reason="retry",
        status="completed",
        quality_score=0.91,
        finished_at_now=True,
    )

    result = execution_result_upsert(
        result_id=f"result-{uuid.uuid4()}",
        execution_id=execution_record["id"],
        attempt_id=attempt["id"],
        is_final=True,
        result_status="completed",
        success=True,
        response_json={"success": True, "markdown": "# ok"},
        meta={"document_type": "invoice", "quality_score": 0.91},
        extracted={"invoice_number": "INV-1"},
        normalized={"invoice_number": "INV-1"},
        artifact_refs={"markdown_path": "/tmp/x.md"},
        warnings=["used_retry"],
        error=None,
    )
    assert result["is_final"] is True
    assert result["result_status"] == "completed"
    assert result["success"] is True

    final_row = execution_result_get_final(execution_record["id"])
    assert final_row is not None
    assert final_row["id"] == result["id"]
    assert final_row["response_json"]["markdown"] == "# ok"
    assert final_row["meta"]["document_type"] == "invoice"
    assert final_row["artifact_refs"]["markdown_path"] == "/tmp/x.md"

    rows = execution_result_list(execution_record["id"])
    assert len(rows) >= 1
    assert rows[0]["execution_id"] == execution_record["id"]


def test_execution_list_contains_newest_execution(execution_record):
    from execution_db import execution_list

    rows = execution_list(limit=10)
    ids = [r["id"] for r in rows]
    assert execution_record["id"] in ids
