"""
Tests für T-DAI-070: Audit Log DB-Schema und audit_db.py Modul.

Nutzt die echte PostgreSQL-DB (daigestr auf localhost:15432).
Die audit_log-Tabelle wird vor jedem Test geTRUNCATEd.
Keine PII in Testdaten.
"""

import os
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extras
import pytest

import audit_db

_DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://daigestr:daigestr@localhost:15432/daigestr",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_audit_table():
    """Sicherstellen dass audit_log-Tabelle existiert und vor jedem Test leer ist."""
    # Tabelle + Indices erstellen falls nicht vorhanden
    audit_db.init_audit_db()

    # Tabelle leeren
    conn = psycopg2.connect(_DB_URL)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("TRUNCATE TABLE audit_log RESTART IDENTITY")
    conn.close()

    yield

    # Cleanup nach Test (optional — nächster Test truncated sowieso)
    conn = psycopg2.connect(_DB_URL)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("TRUNCATE TABLE audit_log RESTART IDENTITY")
    conn.close()


def _pg_conn():
    conn = psycopg2.connect(_DB_URL)
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    return conn


def _count_rows() -> int:
    conn = _pg_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS cnt FROM audit_log")
    result = cur.fetchone()["cnt"]
    conn.close()
    return result


# ---------------------------------------------------------------------------
# Schritt 1: Tabelle existiert nach init_audit_db()
# ---------------------------------------------------------------------------

class TestInitAuditDb:
    """Tabelle und Indices werden korrekt erstellt."""

    def test_table_exists(self):
        """audit_log-Tabelle existiert nach init_audit_db()."""
        conn = _pg_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = 'audit_log'"
        )
        row = cur.fetchone()
        conn.close()
        assert row is not None, "audit_log-Tabelle sollte existieren"

    def test_required_columns_exist(self):
        """Alle Pflichtspalten sind in audit_log vorhanden."""
        conn = _pg_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'audit_log'"
        )
        cols = {r["column_name"] for r in cur.fetchall()}
        conn.close()

        required = {
            "id", "request_id", "execution_id", "job_id", "event_type", "step", "detail",
            "progress", "level", "error", "duration_ms", "metadata",
            "source_ip", "user_agent", "created_at",
        }
        missing = required - cols
        assert not missing, f"Fehlende Spalten: {missing}"

    def test_indices_exist(self):
        """Alle vier Indices sind angelegt."""
        conn = _pg_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'audit_log' AND schemaname = 'public'"
        )
        indices = {r["indexname"] for r in cur.fetchall()}
        conn.close()

        expected = {
            "idx_audit_log_request_id",
            "idx_audit_log_execution_id",
            "idx_audit_log_job_id",
            "idx_audit_log_created_at",
            "idx_audit_log_event_type",
        }
        missing = expected - indices
        assert not missing, f"Fehlende Indices: {missing}"

    def test_init_is_idempotent(self):
        """Mehrfacher Aufruf von init_audit_db() wirft keinen Fehler."""
        audit_db.init_audit_db()
        audit_db.init_audit_db()


# ---------------------------------------------------------------------------
# Schritt 2: audit_log_event schreibt Event
# ---------------------------------------------------------------------------

class TestAuditLogEvent:
    """audit_log_event() schreibt korrekte Daten in die DB."""

    def test_writes_basic_event(self):
        """Einfaches Event wird in die DB geschrieben."""
        audit_db.audit_log_event("req-001", "request")
        assert _count_rows() == 1

    def test_writes_all_fields(self):
        """Alle optionalen Felder werden korrekt gespeichert."""
        audit_db.audit_log_event(
            "req-002",
            "step",
            execution_id="exec-001",
            job_id="job-001",
            step="convert_pdf",
            detail="Seite 1 von 3",
            progress=33,
            level="info",
            error=None,
            duration_ms=250,
            metadata={"pages": 3, "scanned": True},
            source_ip="127.0.0.1",
            user_agent="pytest/test",
        )

        conn = _pg_conn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM audit_log WHERE request_id = 'req-002'")
        row = cur.fetchone()
        conn.close()

        assert row is not None
        assert row["execution_id"] == "exec-001"
        assert row["job_id"] == "job-001"
        assert row["event_type"] == "step"
        assert row["step"] == "convert_pdf"
        assert row["detail"] == "Seite 1 von 3"
        assert row["progress"] == 33
        assert row["level"] == "info"
        assert row["duration_ms"] == 250
        assert row["source_ip"] == "127.0.0.1"
        assert row["user_agent"] == "pytest/test"
        # JSONB wird von psycopg2 als dict zurückgegeben
        assert row["metadata"]["pages"] == 3
        assert row["metadata"]["scanned"] is True

    def test_default_level_is_info(self):
        """Default-Level ist 'info'."""
        audit_db.audit_log_event("req-003", "response")
        conn = _pg_conn()
        cur = conn.cursor()
        cur.execute("SELECT level FROM audit_log WHERE request_id = 'req-003'")
        row = cur.fetchone()
        conn.close()
        assert row["level"] == "info"

    def test_multiple_events_same_request(self):
        """Mehrere Events für dieselbe request_id sind möglich."""
        for event_type in ("request", "step", "response"):
            audit_db.audit_log_event("req-multi", event_type)
        assert _count_rows() == 3

    def test_invalid_event_type_not_written(self):
        """Ungültiger event_type wird abgelehnt — kein Eintrag in DB."""
        audit_db.audit_log_event("req-invalid", "unknown_type")
        assert _count_rows() == 0

    def test_warning_level_event(self):
        """Events mit level='warning' werden korrekt gespeichert."""
        audit_db.audit_log_event(
            "req-warn", "warning", level="warning", error="Rate limit hit"
        )
        conn = _pg_conn()
        cur = conn.cursor()
        cur.execute("SELECT level, error FROM audit_log WHERE request_id = 'req-warn'")
        row = cur.fetchone()
        conn.close()
        assert row["level"] == "warning"
        assert row["error"] == "Rate limit hit"

    def test_mistral_call_event_type(self):
        """event_type='mistral_call' ist gültig."""
        audit_db.audit_log_event("req-mistral", "mistral_call", duration_ms=1200)
        assert _count_rows() == 1


# ---------------------------------------------------------------------------
# Schritt 3: audit_get_by_request findet Events
# ---------------------------------------------------------------------------

class TestAuditGetByRequest:
    """audit_get_by_request() gibt korrekte Events zurück."""

    def test_finds_events_for_request(self):
        """Events für eine bekannte request_id werden gefunden."""
        audit_db.audit_log_event("req-find", "request")
        audit_db.audit_log_event("req-find", "step", step="ocr")
        audit_db.audit_log_event("req-find", "response")

        result = audit_db.audit_get_by_request("req-find")
        assert len(result) == 3

    def test_returns_empty_for_unknown_request(self):
        """Unbekannte request_id gibt leere Liste zurück."""
        result = audit_db.audit_get_by_request("req-nonexistent-xyz")
        assert result == []

    def test_returns_only_matching_request(self):
        """Nur Events der angefragten request_id werden zurückgegeben."""
        audit_db.audit_log_event("req-A", "request")
        audit_db.audit_log_event("req-B", "request")

        result = audit_db.audit_get_by_request("req-A")
        assert len(result) == 1
        assert result[0]["request_id"] == "req-A"

    def test_result_contains_expected_keys(self):
        """Jedes Ergebnis-Dict enthält die erwarteten Schlüssel."""
        audit_db.audit_log_event("req-keys", "request")
        result = audit_db.audit_get_by_request("req-keys")
        assert len(result) == 1
        row = result[0]
        assert "id" in row
        assert "request_id" in row
        assert "event_type" in row
        assert "created_at" in row

    def test_results_ordered_chronologically(self):
        """Ergebnisse sind chronologisch aufsteigend sortiert."""
        for step in ("convert_start", "ocr", "classify"):
            audit_db.audit_log_event("req-order", "step", step=step)

        result = audit_db.audit_get_by_request("req-order")
        steps = [r["step"] for r in result]
        assert steps == ["convert_start", "ocr", "classify"]


# ---------------------------------------------------------------------------
# Schritt 4: audit_get_by_job findet Events
# ---------------------------------------------------------------------------

class TestAuditGetByJob:
    """audit_get_by_job() gibt korrekte Events zurück."""

    def test_finds_events_for_job(self):
        """Events mit einer job_id werden per job gefunden."""
        audit_db.audit_log_event("req-j1", "request", job_id="job-AAA")
        audit_db.audit_log_event("req-j1", "step", job_id="job-AAA", step="convert")
        audit_db.audit_log_event("req-j1", "response", job_id="job-AAA")

        result = audit_db.audit_get_by_job("job-AAA")
        assert len(result) == 3
        for r in result:
            assert r["job_id"] == "job-AAA"

    def test_returns_empty_for_unknown_job(self):
        """Unbekannte job_id gibt leere Liste zurück."""
        result = audit_db.audit_get_by_job("job-nonexistent-xyz")
        assert result == []

    def test_returns_only_matching_job(self):
        """Nur Events der angefragten job_id werden zurückgegeben."""
        audit_db.audit_log_event("req-j2", "request", job_id="job-BBB")
        audit_db.audit_log_event("req-j3", "request", job_id="job-CCC")

        result = audit_db.audit_get_by_job("job-BBB")
        assert len(result) == 1
        assert result[0]["job_id"] == "job-BBB"

    def test_events_without_job_id_not_returned(self):
        """Events ohne job_id werden NICHT von audit_get_by_job zurückgegeben."""
        audit_db.audit_log_event("req-nojob", "request")  # kein job_id
        audit_db.audit_log_event("req-withjob", "request", job_id="job-DDD")

        result = audit_db.audit_get_by_job("job-DDD")
        assert len(result) == 1

    def test_events_can_be_loaded_by_execution_id(self):
        """Events können über execution_id abgefragt werden."""
        audit_db.audit_log_event("req-exec", "request", execution_id="exec-123")
        audit_db.audit_log_event("req-exec", "step", execution_id="exec-123", step="convert")
        audit_db.audit_log_event("req-other", "request", execution_id="exec-999")

        result = audit_db.audit_get_by_execution("exec-123")
        assert len(result) == 2
        assert all(row["execution_id"] == "exec-123" for row in result)


# ---------------------------------------------------------------------------
# Schritt 5: audit_list filtert korrekt
# ---------------------------------------------------------------------------

class TestAuditList:
    """audit_list() filtert nach since, until, level, event_type korrekt."""

    def test_returns_all_without_filters(self):
        """Ohne Filter werden alle Events zurückgegeben (bis limit)."""
        for i in range(5):
            audit_db.audit_log_event(f"req-list-{i}", "request")
        result = audit_db.audit_list(limit=100)
        assert len(result) == 5

    def test_filter_by_event_type(self):
        """Filter event_type gibt nur passende Events zurück."""
        audit_db.audit_log_event("req-et1", "request")
        audit_db.audit_log_event("req-et2", "step", step="ocr")
        audit_db.audit_log_event("req-et3", "response")

        result = audit_db.audit_list(event_type="step")
        assert len(result) == 1
        assert result[0]["event_type"] == "step"

    def test_filter_by_level(self):
        """Filter level gibt nur Events mit passendem Level zurück."""
        audit_db.audit_log_event("req-lvl1", "request", level="info")
        audit_db.audit_log_event("req-lvl2", "warning", level="warning")
        audit_db.audit_log_event("req-lvl3", "response", level="info")

        result = audit_db.audit_list(level="warning")
        assert len(result) == 1
        assert result[0]["level"] == "warning"

    def test_filter_since(self):
        """Filter since schließt ältere Events aus."""
        # Füge einen Event "in der Vergangenheit" via direktem INSERT ein
        conn = psycopg2.connect(_DB_URL)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO audit_log (request_id, event_type, created_at) "
            "VALUES ('req-old', 'request', now() - interval '2 days')"
        )
        conn.close()

        # Aktueller Event
        audit_db.audit_log_event("req-now", "request")

        since = datetime.now(tz=timezone.utc) - timedelta(hours=1)
        result = audit_db.audit_list(since=since, limit=100)

        request_ids = {r["request_id"] for r in result}
        assert "req-now" in request_ids
        assert "req-old" not in request_ids

    def test_filter_until(self):
        """Filter until schließt neuere Events aus."""
        # Füge einen "alten" Event direkt ein
        conn = psycopg2.connect(_DB_URL)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO audit_log (request_id, event_type, created_at) "
            "VALUES ('req-very-old', 'request', now() - interval '5 days')"
        )
        conn.close()

        # Aktueller Event
        audit_db.audit_log_event("req-current", "request")

        until = datetime.now(tz=timezone.utc) - timedelta(days=1)
        result = audit_db.audit_list(until=until, limit=100)

        request_ids = {r["request_id"] for r in result}
        assert "req-very-old" in request_ids
        assert "req-current" not in request_ids

    def test_limit_is_respected(self):
        """limit begrenzt die Anzahl zurückgegebener Events."""
        for i in range(10):
            audit_db.audit_log_event(f"req-lim-{i}", "request")

        result = audit_db.audit_list(limit=3)
        assert len(result) == 3

    def test_results_ordered_newest_first(self):
        """Ergebnisse sind neueste zuerst sortiert."""
        for i in range(3):
            audit_db.audit_log_event(f"req-ord-{i}", "request")

        result = audit_db.audit_list(limit=10)
        ids = [r["id"] for r in result]
        # IDs sind aufsteigend — neueste haben höchste ID → DESC-Sortierung
        assert ids == sorted(ids, reverse=True)

    def test_combined_filters(self):
        """Kombination von event_type + level funktioniert korrekt."""
        audit_db.audit_log_event("req-c1", "step", level="info")
        audit_db.audit_log_event("req-c2", "step", level="warning")
        audit_db.audit_log_event("req-c3", "response", level="warning")

        result = audit_db.audit_list(event_type="step", level="warning")
        assert len(result) == 1
        assert result[0]["request_id"] == "req-c2"


# ---------------------------------------------------------------------------
# Schritt 6: audit_cleanup löscht alte Einträge
# ---------------------------------------------------------------------------

class TestAuditCleanup:
    """audit_cleanup() löscht korrekt alte Einträge."""

    def test_cleanup_deletes_old_entries(self):
        """Einträge älter als retention_days werden gelöscht."""
        conn = psycopg2.connect(_DB_URL)
        conn.autocommit = True
        cur = conn.cursor()
        # 2 alte Einträge (35 Tage alt)
        cur.execute(
            "INSERT INTO audit_log (request_id, event_type, created_at) "
            "VALUES ('req-old1', 'request', now() - interval '35 days'), "
            "       ('req-old2', 'request', now() - interval '40 days')"
        )
        conn.close()

        # 1 neuer Eintrag
        audit_db.audit_log_event("req-new", "request")

        assert _count_rows() == 3

        deleted = audit_db.audit_cleanup(retention_days=30)
        assert deleted == 2
        assert _count_rows() == 1

    def test_cleanup_returns_zero_if_nothing_to_delete(self):
        """Wenn keine alten Einträge vorhanden, gibt cleanup 0 zurück."""
        audit_db.audit_log_event("req-fresh", "request")
        deleted = audit_db.audit_cleanup(retention_days=30)
        assert deleted == 0

    def test_cleanup_does_not_delete_recent_entries(self):
        """Neue Einträge werden nicht von cleanup gelöscht."""
        for i in range(3):
            audit_db.audit_log_event(f"req-recent-{i}", "request")

        deleted = audit_db.audit_cleanup(retention_days=30)
        assert deleted == 0
        assert _count_rows() == 3

    def test_cleanup_with_retention_zero_deletes_all(self):
        """retention_days=0 löscht alle Einträge."""
        for i in range(5):
            audit_db.audit_log_event(f"req-all-{i}", "request")

        deleted = audit_db.audit_cleanup(retention_days=0)
        assert deleted == 5
        assert _count_rows() == 0

    def test_cleanup_returns_int(self):
        """Rückgabewert ist immer ein int."""
        result = audit_db.audit_cleanup(retention_days=30)
        assert isinstance(result, int)
