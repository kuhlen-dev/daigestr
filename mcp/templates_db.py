"""
Template Registry — SQLite DB Helpers (T-MKIT-035).

Enthält die SQLite-Logik für das Template-Registry:
init_templates_db, get_all_template_ids, search_templates.

Zusätzlich (T-DAI-008): Prompts und Scoring-Gewichtungen:
get_prompt, get_scoring_weight, upsert_prompt, list_prompts, get_prompt_by_id.

get_db_connection und get_template_by_id werden aus intelligence.py importiert
(sie sind dort definiert um zirkuläre Imports zu vermeiden).

Patchbare Symbole werden über _get() aus dem server-Namespace gelesen.
"""

import json
import sqlite3
from pathlib import Path
from typing import Optional

import structlog

from settings import TEMPLATES_DB_PATH
from utils import _get
from intelligence import get_db_connection, get_template_by_id  # noqa: F401 — re-exported

log = structlog.get_logger()


def init_templates_db() -> None:
    """Erstellt die templates.db beim ersten Start und migriert bestehende Templates."""
    _db_path = _get("TEMPLATES_DB_PATH", TEMPLATES_DB_PATH)
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("""
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
            enabled BOOLEAN DEFAULT 1,
            version INTEGER DEFAULT 1,
            source TEXT DEFAULT 'manual',
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_templates_category ON template(category)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_templates_enabled ON template(enabled)")

    # Prompts-Tabelle (T-DAI-008)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS prompt (
            id TEXT PRIMARY KEY,
            category TEXT NOT NULL,
            name TEXT NOT NULL,
            content_de TEXT,
            content_en TEXT,
            version INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_prompts_category ON prompt(category)")

    # Scoring-Gewichtungen-Tabelle (T-DAI-008)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scoring_weight (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            value REAL NOT NULL,
            description TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    # Request-Level-Cache (T-DAI-019)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cache (
            key TEXT PRIMARY KEY,
            response_json TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    # Async Job Tracking (T-DAI-023)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS job (
            id TEXT PRIMARY KEY,
            status TEXT DEFAULT 'queued',
            progress_json TEXT,
            result_json TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)

    # Seed aus seed.sql wenn DB leer (template-Tabelle prüfen)
    cursor = conn.execute("SELECT COUNT(*) FROM template")
    if cursor.fetchone()[0] == 0:
        seed_path = Path(__file__).parent / "seed.sql"
        if seed_path.exists():
            seed_sql = seed_path.read_text(encoding="utf-8")
            conn.executescript(seed_sql)
            log.info("templates_seeded", source=str(seed_path))
    conn.commit()
    conn.close()


def get_all_template_ids() -> list[str]:
    """Gibt alle aktiven Template-IDs aus der DB zurück."""
    _get_db_conn = _get("get_db_connection", get_db_connection)
    conn = _get_db_conn()
    rows = conn.execute("SELECT id FROM template WHERE enabled = 1 ORDER BY category, display_name").fetchall()
    conn.close()
    return [r["id"] for r in rows]


def search_templates(query: str) -> list[dict]:
    """Sucht Templates nach Stichwort."""
    _get_db_conn = _get("get_db_connection", get_db_connection)
    conn = _get_db_conn()
    rows = conn.execute(
        "SELECT id, category, display_name, description, enabled FROM template "
        "WHERE id LIKE ? OR display_name LIKE ? OR description LIKE ? OR classify_keywords LIKE ?",
        (f"%{query}%", f"%{query}%", f"%{query}%", f"%{query}%")
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# =============================================================================
# Prompts — T-DAI-008
# =============================================================================

def get_prompt(category: str, name: str, language: str = "de") -> str:
    """Lädt einen Prompt aus der DB. Wirft ValueError wenn nicht gefunden — KEIN Fallback!

    Args:
        category: Prompt-Kategorie (z.B. 'vision.system', 'classify.system_de').
        name: Prompt-Name (z.B. 'system', 'user').
        language: 'de' oder 'en' — bestimmt welche content_* Spalte genutzt wird.

    Returns:
        Prompt-Text als String.

    Raises:
        ValueError: Wenn der Prompt nicht gefunden oder der Inhalt leer ist.
    """
    _get_db_conn = _get("get_db_connection", get_db_connection)
    conn = _get_db_conn()
    row = conn.execute(
        "SELECT content_de, content_en FROM prompt WHERE category = ? AND name = ?",
        (category, name)
    ).fetchone()
    conn.close()

    if not row:
        raise ValueError(f"Prompt nicht gefunden: category='{category}' name='{name}'")

    content = row["content_de"] if language == "de" else row["content_en"]
    # Fallback auf die andere Sprache wenn gewünschte leer
    if not content:
        content = row["content_en"] if language == "de" else row["content_de"]
    if not content:
        raise ValueError(
            f"Prompt-Inhalt leer: category='{category}' name='{name}' language='{language}'"
        )
    return content


def get_prompt_by_id(prompt_id: str) -> dict:
    """Lädt einen Prompt per ID. Wirft ValueError wenn nicht gefunden.

    Returns:
        Dict mit id, category, name, content_de, content_en, version.
    """
    _get_db_conn = _get("get_db_connection", get_db_connection)
    conn = _get_db_conn()
    row = conn.execute("SELECT * FROM prompt WHERE id = ?", (prompt_id,)).fetchone()
    conn.close()
    if not row:
        raise ValueError(f"Prompt nicht gefunden: id='{prompt_id}'")
    return dict(row)


def list_prompts(category: Optional[str] = None) -> list[dict]:
    """Gibt alle Prompts zurück, optional gefiltert nach Kategorie."""
    _get_db_conn = _get("get_db_connection", get_db_connection)
    conn = _get_db_conn()
    if category:
        rows = conn.execute(
            "SELECT id, category, name, version FROM prompt WHERE category = ? ORDER BY category, name",
            (category,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, category, name, version FROM prompt ORDER BY category, name"
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def upsert_prompt(
    prompt_id: str,
    category: str,
    name: str,
    content_de: Optional[str] = None,
    content_en: Optional[str] = None,
) -> dict:
    """Erstellt oder aktualisiert einen Prompt (inkrementiert Version bei Update).

    Returns:
        Dict mit id, category, name, version.
    """
    _get_db_conn = _get("get_db_connection", get_db_connection)
    conn = _get_db_conn()
    existing = conn.execute("SELECT version FROM prompt WHERE id = ?", (prompt_id,)).fetchone()

    if existing:
        new_version = existing["version"] + 1
        conn.execute(
            "UPDATE prompt SET category=?, name=?, content_de=?, content_en=?, "
            "version=?, updated_at=datetime('now') WHERE id=?",
            (category, name, content_de, content_en, new_version, prompt_id)
        )
    else:
        new_version = 1
        conn.execute(
            "INSERT INTO prompt (id, category, name, content_de, content_en, version) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (prompt_id, category, name, content_de, content_en)
        )

    conn.commit()
    conn.close()
    return {"id": prompt_id, "category": category, "name": name, "version": new_version}


# =============================================================================
# Scoring Weights — T-DAI-008
# =============================================================================

# =============================================================================
# Request-Level-Cache — T-DAI-019
# =============================================================================

def cache_get(key: str, ttl: int) -> Optional[str]:
    """Gibt gecachte Response zurück wenn vorhanden und nicht abgelaufen, sonst None.

    Args:
        key: Cache-Key (SHA256-Hash).
        ttl: Time-to-live in Sekunden.

    Returns:
        response_json als String oder None bei Cache-Miss / abgelaufenen Einträgen.
    """
    _get_db_conn = _get("get_db_connection", get_db_connection)
    conn = _get_db_conn()
    try:
        row = conn.execute(
            "SELECT response_json FROM cache WHERE key = ? "
            "AND created_at > datetime('now', '-' || ? || ' seconds')",
            (key, str(ttl))
        ).fetchone()
    except sqlite3.OperationalError:
        conn.close()
        return None  # Tabelle existiert nicht → kein Cache-Hit
    conn.close()
    if row:
        return row["response_json"]
    return None


def cache_set(key: str, response_json: str) -> None:
    """Speichert eine Response im Cache (INSERT OR REPLACE).

    Args:
        key: Cache-Key (SHA256-Hash).
        response_json: Serialisierte JSON-Response als String.
    """
    _get_db_conn = _get("get_db_connection", get_db_connection)
    conn = _get_db_conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO cache (key, response_json) VALUES (?, ?)",
            (key, response_json)
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Tabelle existiert nicht → silently skip
    conn.close()


def cache_clear() -> None:
    """Löscht alle Einträge aus dem Cache."""
    _get_db_conn = _get("get_db_connection", get_db_connection)
    conn = _get_db_conn()
    conn.execute("DELETE FROM cache")
    conn.commit()
    conn.close()


# =============================================================================
# Async Job API — T-DAI-023
# =============================================================================

def job_create(job_id: str) -> dict:
    """Erstellt einen neuen Job mit Status 'queued'.

    Args:
        job_id: Eindeutige Job-ID (UUID).

    Returns:
        Dict mit job_id und status.
    """
    _get_db_conn = _get("get_db_connection", get_db_connection)
    conn = _get_db_conn()
    conn.execute(
        "INSERT INTO job (id, status) VALUES (?, 'queued')",
        (job_id,)
    )
    conn.commit()
    conn.close()
    return {"job_id": job_id, "status": "queued"}


def job_update(job_id: str, status: str, progress_json: Optional[str] = None) -> None:
    """Aktualisiert Status und Fortschritt eines Jobs.

    Args:
        job_id: Job-ID.
        status: Neuer Status (z.B. 'processing', 'completed', 'failed').
        progress_json: Optional serialisiertes Fortschritts-Objekt (JSON-String).
    """
    _get_db_conn = _get("get_db_connection", get_db_connection)
    conn = _get_db_conn()
    conn.execute(
        "UPDATE job SET status=?, progress_json=?, updated_at=datetime('now') WHERE id=?",
        (status, progress_json, job_id)
    )
    conn.commit()
    conn.close()


def job_set_result(job_id: str, result_json: str) -> None:
    """Setzt das Ergebnis eines abgeschlossenen Jobs.

    Args:
        job_id: Job-ID.
        result_json: Serialisiertes ConvertResponse als JSON-String.
    """
    _get_db_conn = _get("get_db_connection", get_db_connection)
    conn = _get_db_conn()
    conn.execute(
        "UPDATE job SET result_json=?, status='completed', updated_at=datetime('now') WHERE id=?",
        (result_json, job_id)
    )
    conn.commit()
    conn.close()


def job_get(job_id: str) -> Optional[dict]:
    """Lädt einen Job per ID.

    Args:
        job_id: Job-ID.

    Returns:
        Dict mit id, status, progress_json, result_json, created_at, updated_at
        oder None wenn nicht gefunden.
    """
    _get_db_conn = _get("get_db_connection", get_db_connection)
    conn = _get_db_conn()
    row = conn.execute("SELECT * FROM job WHERE id=?", (job_id,)).fetchone()
    conn.close()
    if not row:
        return None
    return dict(row)


def job_delete(job_id: str) -> bool:
    """Löscht einen Job.

    Args:
        job_id: Job-ID.

    Returns:
        True wenn gelöscht, False wenn nicht gefunden.
    """
    _get_db_conn = _get("get_db_connection", get_db_connection)
    conn = _get_db_conn()
    cursor = conn.execute("DELETE FROM job WHERE id=?", (job_id,))
    conn.commit()
    conn.close()
    return cursor.rowcount > 0


def job_list() -> list[dict]:
    """Gibt alle Jobs zurück, neueste zuerst.

    Returns:
        Liste von Dicts mit id, status, created_at, updated_at (ohne result_json für Kompaktheit).
    """
    _get_db_conn = _get("get_db_connection", get_db_connection)
    conn = _get_db_conn()
    rows = conn.execute(
        "SELECT id, status, progress_json, created_at, updated_at FROM job ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_scoring_weight(name: str) -> float:
    """Lädt eine Scoring-Gewichtung aus der DB. Wirft ValueError wenn nicht gefunden — KEIN Fallback!

    Args:
        name: Name der Gewichtung (z.B. 'density_low_threshold').

    Returns:
        Gewichtungs-Wert als float.

    Raises:
        ValueError: Wenn die Gewichtung nicht gefunden wird.
    """
    _get_db_conn = _get("get_db_connection", get_db_connection)
    conn = _get_db_conn()
    row = conn.execute("SELECT value FROM scoring_weight WHERE id = ?", (name,)).fetchone()
    conn.close()
    if not row:
        raise ValueError(f"Scoring-Gewichtung nicht gefunden: '{name}'")
    return float(row["value"])
