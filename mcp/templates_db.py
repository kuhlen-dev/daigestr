"""
Template Registry — PostgreSQL DB Helpers (T-DAI-032).

Enthält die PostgreSQL-Logik für das Template-Registry:
init_templates_db, get_all_template_ids, search_templates.

Zusätzlich (T-DAI-008): Prompts und Scoring-Gewichtungen:
get_prompt, get_scoring_weight, upsert_prompt, list_prompts, get_prompt_by_id.

get_db_connection gibt eine Verbindung aus dem Pool zurück (RealDictCursor).
pool_reset() für Tests.
"""

import json
from pathlib import Path
from typing import Optional

import psycopg2
import psycopg2.extras
import psycopg2.pool
import structlog

from settings import DATABASE_URL, DB_POOL_MIN, DB_POOL_MAX
from utils import _get

log = structlog.get_logger()

# Connection Pool (initialisiert bei erstem Import)
_pool: Optional[psycopg2.pool.SimpleConnectionPool] = None


def _get_pool() -> psycopg2.pool.SimpleConnectionPool:
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.SimpleConnectionPool(
            DB_POOL_MIN,
            DB_POOL_MAX,
            DATABASE_URL,
        )
    return _pool


def pool_reset() -> None:
    """Setzt den Connection-Pool zurück (für Tests)."""
    global _pool
    if _pool is not None:
        try:
            _pool.closeall()
        except Exception:
            pass
        _pool = None


def get_db_connection():
    """Gibt eine Verbindung aus dem Pool zurück (RealDictCursor als row_factory)."""
    pool = _get_pool()
    conn = pool.getconn()
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    return conn


def _return_conn(conn) -> None:
    """Gibt eine Verbindung zurück in den Pool.

    Rollt offene Transaktionen zurück bevor die Verbindung in den Pool
    zurückgegeben wird — verhindert 'idle in transaction'-Locks.
    STATUS_READY=1 (psycopg2.extensions.STATUS_READY).
    """
    try:
        if conn.status != 1:  # nicht STATUS_READY → offene Transaktion
            conn.rollback()
    except Exception:
        pass
    try:
        _get_pool().putconn(conn)
    except Exception:
        pass


def check_persistence_health() -> dict:
    """
    Verify PostgreSQL connectivity and presence of critical Daigestr tables.

    Returns a small health payload that can be exposed via /v1/health without
    duplicating DB-specific logic in higher layers.
    """
    required_tables = [
        "template",
        "prompt",
        "scoring_weight",
        "cache",
        "job",
        "audit_log",
        "normalized_categories",
        "normalized_fields",
        "normalized_values",
        "normalized_test_fixtures",
        "extraction_corrections",
    ]

    _get_db_conn = _get("get_db_connection", get_db_connection)
    conn = None
    try:
        conn = _get_db_conn()
        cur = conn.cursor()
        cur.execute("SELECT 1 AS ok")
        cur.fetchone()
        cur.execute(
            "SELECT tablename FROM pg_tables "
            "WHERE schemaname = 'public' AND tablename = ANY(%s)",
            (required_tables,),
        )
        rows = cur.fetchall()
        present_tables = sorted(r["tablename"] for r in rows)
        missing_tables = sorted(set(required_tables) - set(present_tables))
        return {
            "ready": not missing_tables,
            "database_url_configured": bool(DATABASE_URL),
            "connection_ok": True,
            "required_tables_checked": required_tables,
            "present_tables": present_tables,
            "missing_tables": missing_tables,
        }
    except Exception as exc:
        return {
            "ready": False,
            "database_url_configured": bool(DATABASE_URL),
            "connection_ok": False,
            "required_tables_checked": required_tables,
            "present_tables": [],
            "missing_tables": required_tables,
            "error": str(exc),
        }
    finally:
        if conn is not None:
            _return_conn(conn)


def init_templates_db() -> None:
    """Erstellt die Tabellen beim ersten Start und lädt seed.sql wenn leer."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS template (
                id TEXT PRIMARY KEY,
                category TEXT NOT NULL DEFAULT 'other',
                display_name TEXT NOT NULL,
                description TEXT,
                schema TEXT NOT NULL,
                field_descriptions TEXT,
                classify_keywords TEXT,
                typical_senders TEXT,
                steuer_relevanz TEXT,
                priority INTEGER DEFAULT 0,
                enabled SMALLINT DEFAULT 1,
                version INTEGER DEFAULT 1,
                source TEXT DEFAULT 'manual',
                notes TEXT,
                created_at TIMESTAMP DEFAULT now(),
                updated_at TIMESTAMP DEFAULT now()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_templates_category ON template(category)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_templates_enabled ON template(enabled)")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS prompt (
                id TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                name TEXT NOT NULL,
                content_de TEXT,
                content_en TEXT,
                version INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT now(),
                updated_at TIMESTAMP DEFAULT now()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_prompts_category ON prompt(category)")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS scoring_weight (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                value REAL NOT NULL,
                description TEXT,
                created_at TIMESTAMP DEFAULT now()
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS cache (
                key TEXT PRIMARY KEY,
                response_json TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT now()
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS job (
                id TEXT PRIMARY KEY,
                status TEXT DEFAULT 'queued',
                progress_json TEXT,
                result_json TEXT,
                created_at TIMESTAMP DEFAULT now(),
                updated_at TIMESTAMP DEFAULT now()
            )
        """)

        conn.commit()

        # Seed wenn template-Tabelle leer
        cur.execute("SELECT COUNT(*) AS cnt FROM template")
        row = cur.fetchone()
        if row["cnt"] == 0:
            seed_path = Path(__file__).parent / "seed.sql"
            if seed_path.exists():
                seed_sql = seed_path.read_text(encoding="utf-8")
                cur.execute(seed_sql)
                conn.commit()
                log.info("templates_seeded", source=str(seed_path))
    finally:
        _return_conn(conn)


def get_all_template_ids() -> list[str]:
    """Gibt alle aktiven Template-IDs aus der DB zurück."""
    _get_db_conn = _get("get_db_connection", get_db_connection)
    conn = _get_db_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM template WHERE enabled = 1 ORDER BY category, display_name")
        rows = cur.fetchall()
        return [r["id"] for r in rows]
    finally:
        _return_conn(conn)


def get_template_by_id(template_id: str) -> Optional[dict]:
    """Lädt ein Template aus der DB. Gibt None zurück wenn nicht gefunden oder disabled."""
    _get_db_conn = _get("get_db_connection", get_db_connection)
    conn = _get_db_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM template WHERE id = %s AND enabled = 1", (template_id,))
        row = cur.fetchone()
        if not row:
            return None
        result = dict(row)
        result["schema"] = json.loads(result["schema"])
        if result.get("field_descriptions"):
            try:
                result["field_descriptions"] = json.loads(result["field_descriptions"])
            except (json.JSONDecodeError, TypeError):
                pass
        return result
    finally:
        _return_conn(conn)


def search_templates(query: str) -> list[dict]:
    """Sucht Templates nach Stichwort."""
    _get_db_conn = _get("get_db_connection", get_db_connection)
    conn = _get_db_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, category, display_name, description, enabled FROM template "
            "WHERE id LIKE %s OR display_name LIKE %s OR description LIKE %s OR classify_keywords LIKE %s",
            (f"%{query}%", f"%{query}%", f"%{query}%", f"%{query}%")
        )
        rows = cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        _return_conn(conn)


# =============================================================================
# Prompts — T-DAI-008
# =============================================================================

def get_prompt(category: str, name: str, language: str = "de") -> str:
    """Lädt einen Prompt aus der DB. Wirft ValueError wenn nicht gefunden."""
    _get_db_conn = _get("get_db_connection", get_db_connection)
    conn = _get_db_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT content_de, content_en FROM prompt WHERE category = %s AND name = %s",
            (category, name)
        )
        row = cur.fetchone()
    finally:
        _return_conn(conn)

    if not row:
        raise ValueError(f"Prompt nicht gefunden: category='{category}' name='{name}'")

    content = row["content_de"] if language == "de" else row["content_en"]
    if not content:
        content = row["content_en"] if language == "de" else row["content_de"]
    if not content:
        raise ValueError(
            f"Prompt-Inhalt leer: category='{category}' name='{name}' language='{language}'"
        )
    return content


def get_prompt_by_id(prompt_id: str) -> dict:
    """Lädt einen Prompt per ID. Wirft ValueError wenn nicht gefunden."""
    _get_db_conn = _get("get_db_connection", get_db_connection)
    conn = _get_db_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM prompt WHERE id = %s", (prompt_id,))
        row = cur.fetchone()
    finally:
        _return_conn(conn)
    if not row:
        raise ValueError(f"Prompt nicht gefunden: id='{prompt_id}'")
    return dict(row)


def list_prompts(category: Optional[str] = None) -> list[dict]:
    """Gibt alle Prompts zurück, optional gefiltert nach Kategorie."""
    _get_db_conn = _get("get_db_connection", get_db_connection)
    conn = _get_db_conn()
    try:
        cur = conn.cursor()
        if category:
            cur.execute(
                "SELECT id, category, name, version FROM prompt WHERE category = %s ORDER BY category, name",
                (category,)
            )
        else:
            cur.execute(
                "SELECT id, category, name, version FROM prompt ORDER BY category, name"
            )
        rows = cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        _return_conn(conn)


def upsert_prompt(
    prompt_id: str,
    category: str,
    name: str,
    content_de: Optional[str] = None,
    content_en: Optional[str] = None,
) -> dict:
    """Erstellt oder aktualisiert einen Prompt (inkrementiert Version bei Update)."""
    _get_db_conn = _get("get_db_connection", get_db_connection)
    conn = _get_db_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT version FROM prompt WHERE id = %s", (prompt_id,))
        existing = cur.fetchone()

        if existing:
            new_version = existing["version"] + 1
            cur.execute(
                "UPDATE prompt SET category=%s, name=%s, content_de=%s, content_en=%s, "
                "version=%s, updated_at=now() WHERE id=%s",
                (category, name, content_de, content_en, new_version, prompt_id)
            )
        else:
            new_version = 1
            cur.execute(
                "INSERT INTO prompt (id, category, name, content_de, content_en, version) "
                "VALUES (%s, %s, %s, %s, %s, 1)",
                (prompt_id, category, name, content_de, content_en)
            )

        conn.commit()
    finally:
        _return_conn(conn)
    return {"id": prompt_id, "category": category, "name": name, "version": new_version}


# =============================================================================
# Scoring Weights — T-DAI-008
# =============================================================================

def get_scoring_weight(name: str) -> float:
    """Lädt eine Scoring-Gewichtung aus der DB. Wirft ValueError wenn nicht gefunden."""
    _get_db_conn = _get("get_db_connection", get_db_connection)
    conn = _get_db_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT value FROM scoring_weight WHERE id = %s", (name,))
        row = cur.fetchone()
    finally:
        _return_conn(conn)
    if not row:
        raise ValueError(f"Scoring-Gewichtung nicht gefunden: '{name}'")
    return float(row["value"])


# =============================================================================
# Request-Level-Cache — T-DAI-019
# =============================================================================

def cache_get(key: str, ttl: int) -> Optional[str]:
    """Gibt gecachte Response zurück wenn vorhanden und nicht abgelaufen, sonst None."""
    _get_db_conn = _get("get_db_connection", get_db_connection)
    conn = _get_db_conn()
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT response_json FROM cache WHERE key = %s "
                "AND created_at > now() - (interval '1 second' * %s)",
                (key, ttl)
            )
            row = cur.fetchone()
        except psycopg2.errors.UndefinedTable:
            conn.rollback()
            return None
    finally:
        _return_conn(conn)
    if row:
        return row["response_json"]
    return None


def cache_set(key: str, response_json: str) -> None:
    """Speichert eine Response im Cache (INSERT ... ON CONFLICT DO UPDATE)."""
    _get_db_conn = _get("get_db_connection", get_db_connection)
    conn = _get_db_conn()
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                "INSERT INTO cache (key, response_json) VALUES (%s, %s) "
                "ON CONFLICT (key) DO UPDATE SET response_json=EXCLUDED.response_json, created_at=now()",
                (key, response_json)
            )
            conn.commit()
        except psycopg2.errors.UndefinedTable:
            conn.rollback()
    finally:
        _return_conn(conn)


def cache_clear() -> None:
    """Löscht alle Einträge aus dem Cache."""
    _get_db_conn = _get("get_db_connection", get_db_connection)
    conn = _get_db_conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM cache")
        conn.commit()
    finally:
        _return_conn(conn)


# =============================================================================
# Async Job API — T-DAI-023
# =============================================================================

def job_create(job_id: str) -> dict:
    """Erstellt einen neuen Job mit Status 'queued'."""
    _get_db_conn = _get("get_db_connection", get_db_connection)
    conn = _get_db_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO job (id, status) VALUES (%s, 'queued')",
            (job_id,)
        )
        conn.commit()
    finally:
        _return_conn(conn)
    return {"job_id": job_id, "status": "queued"}


def job_update(job_id: str, status: str, progress_json: Optional[str] = None) -> None:
    """Aktualisiert Status und Fortschritt eines Jobs."""
    _get_db_conn = _get("get_db_connection", get_db_connection)
    conn = _get_db_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE job SET status=%s, progress_json=%s, updated_at=now() WHERE id=%s",
            (status, progress_json, job_id)
        )
        conn.commit()
    finally:
        _return_conn(conn)


def job_set_result(job_id: str, result_json: str) -> None:
    """Setzt das Ergebnis eines abgeschlossenen Jobs."""
    _get_db_conn = _get("get_db_connection", get_db_connection)
    conn = _get_db_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE job SET result_json=%s, status='completed', updated_at=now() WHERE id=%s",
            (result_json, job_id)
        )
        conn.commit()
    finally:
        _return_conn(conn)


def job_get(job_id: str) -> Optional[dict]:
    """Lädt einen Job per ID."""
    _get_db_conn = _get("get_db_connection", get_db_connection)
    conn = _get_db_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM job WHERE id=%s", (job_id,))
        row = cur.fetchone()
    finally:
        _return_conn(conn)
    if not row:
        return None
    return dict(row)


def job_delete(job_id: str) -> bool:
    """Löscht einen Job. Gibt True zurück wenn gelöscht."""
    _get_db_conn = _get("get_db_connection", get_db_connection)
    conn = _get_db_conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM job WHERE id=%s", (job_id,))
        deleted = cur.rowcount > 0
        conn.commit()
    finally:
        _return_conn(conn)
    return deleted


def job_list() -> list[dict]:
    """Gibt alle Jobs zurück, neueste zuerst."""
    _get_db_conn = _get("get_db_connection", get_db_connection)
    conn = _get_db_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, status, progress_json, created_at, updated_at FROM job ORDER BY created_at DESC"
        )
        rows = cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        _return_conn(conn)
