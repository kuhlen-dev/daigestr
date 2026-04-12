"""
Tests for T-DAI-083: debug snapshot PostgreSQL persistence.
"""

import os
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extras
import pytest

import debug_snapshot_db as snapshot_db

_DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://daigestr:daigestr@localhost:15432/daigestr",
)


@pytest.fixture(autouse=True)
def reset_snapshot_table():
    snapshot_db.init_debug_snapshot_db()
    conn = psycopg2.connect(_DB_URL)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("TRUNCATE TABLE debug_snapshot RESTART IDENTITY")
    conn.close()
    yield
    conn = psycopg2.connect(_DB_URL)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("TRUNCATE TABLE debug_snapshot RESTART IDENTITY")
    conn.close()


def _pg_conn():
    conn = psycopg2.connect(_DB_URL)
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    return conn


class TestInitDebugSnapshotDb:
    def test_table_exists(self):
        conn = _pg_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = 'debug_snapshot'"
        )
        row = cur.fetchone()
        conn.close()
        assert row is not None

    def test_required_columns_exist(self):
        conn = _pg_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'debug_snapshot'"
        )
        cols = {r["column_name"] for r in cur.fetchall()}
        conn.close()
        required = {
            "id",
            "request_id",
            "job_id",
            "stage",
            "attempt_number",
            "attempt_count",
            "attempt_mode",
            "filename",
            "source_type",
            "payload_json",
            "created_at",
            "expires_at",
        }
        assert not (required - cols)

    def test_indices_exist(self):
        conn = _pg_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'debug_snapshot' AND schemaname = 'public'"
        )
        indices = {r["indexname"] for r in cur.fetchall()}
        conn.close()
        expected = {
            "idx_debug_snapshot_request_id",
            "idx_debug_snapshot_job_id",
            "idx_debug_snapshot_stage",
            "idx_debug_snapshot_created_at",
            "idx_debug_snapshot_expires_at",
        }
        assert not (expected - indices)


class TestDebugSnapshotCrud:
    def test_store_and_get_snapshot(self):
        snapshot_id = snapshot_db.debug_snapshot_store(
            request_id="req-1",
            job_id="job-1",
            stage="extract",
            attempt_number=2,
            attempt_count=2,
            attempt_mode="full",
            filename="scan.pdf",
            source_type="base64",
            payload={"meta": {"quality_score": 0.71}, "markdown": "# text"},
            retention_days=3,
        )

        row = snapshot_db.debug_snapshot_get(snapshot_id)
        assert row is not None
        assert row["request_id"] == "req-1"
        assert row["job_id"] == "job-1"
        assert row["stage"] == "extract"
        assert row["attempt_number"] == 2
        assert row["payload_json"]["meta"]["quality_score"] == 0.71

    def test_list_filters_by_request(self):
        snapshot_db.debug_snapshot_store(
            request_id="req-a",
            job_id="job-1",
            stage="markdown",
            payload={"markdown": "# A"},
        )
        snapshot_db.debug_snapshot_store(
            request_id="req-b",
            job_id="job-2",
            stage="normalized",
            payload={"normalized": {"x": 1}},
        )

        rows = snapshot_db.debug_snapshot_list(request_id="req-a")
        assert len(rows) == 1
        assert rows[0]["request_id"] == "req-a"
        assert rows[0]["stage"] == "markdown"

    def test_list_filters_by_job_stage_and_limit(self):
        snapshot_db.debug_snapshot_store(
            request_id="req-1",
            job_id="job-focus",
            stage="extract_result",
            payload={"markdown": "# A"},
        )
        snapshot_db.debug_snapshot_store(
            request_id="req-2",
            job_id="job-focus",
            stage="extract_result",
            payload={"markdown": "# B"},
        )
        snapshot_db.debug_snapshot_store(
            request_id="req-3",
            job_id="job-other",
            stage="normalized_result",
            payload={"markdown": "# C"},
        )

        rows = snapshot_db.debug_snapshot_list(job_id="job-focus", stage="extract_result", limit=1)
        assert len(rows) == 1
        assert rows[0]["job_id"] == "job-focus"
        assert rows[0]["stage"] == "extract_result"

    def test_cleanup_removes_expired_snapshots(self):
        snapshot_db.debug_snapshot_store(
            request_id="req-expired",
            stage="extract",
            payload={"x": 1},
            expires_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        snapshot_db.debug_snapshot_store(
            request_id="req-fresh",
            stage="extract",
            payload={"x": 2},
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        )

        deleted = snapshot_db.debug_snapshot_cleanup()
        rows = snapshot_db.debug_snapshot_list(limit=10)

        assert deleted == 1
        assert len(rows) == 1
        assert rows[0]["request_id"] == "req-fresh"

    def test_cleanup_retention_days_removes_old_rows(self):
        snapshot_db.debug_snapshot_store(
            request_id="req-old",
            stage="extract",
            payload={"x": 1},
        )
        snapshot_db.debug_snapshot_store(
            request_id="req-new",
            stage="extract",
            payload={"x": 2},
        )

        conn = _pg_conn()
        cur = conn.cursor()
        cur.execute(
            "UPDATE debug_snapshot SET created_at = now() - interval '10 days' WHERE request_id = %s",
            ("req-old",),
        )
        conn.commit()
        conn.close()

        deleted = snapshot_db.debug_snapshot_cleanup(retention_days=3)
        rows = snapshot_db.debug_snapshot_list(limit=10)

        assert deleted == 1
        assert len(rows) == 1
        assert rows[0]["request_id"] == "req-new"
