"""
Template Registry — SQLite DB Helpers (T-MKIT-035).

Enthält die SQLite-Logik für das Template-Registry:
init_templates_db, get_all_template_ids, search_templates.

get_db_connection und get_template_by_id werden aus intelligence.py importiert
(sie sind dort definiert um zirkuläre Imports zu vermeiden).

Patchbare Symbole werden über _get() aus dem server-Namespace gelesen.
"""

import json
import sqlite3
from pathlib import Path

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

    # Seed aus seed.sql wenn DB leer
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
