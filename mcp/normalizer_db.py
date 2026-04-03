"""
Normalization DB — PostgreSQL helpers for field normalization (T-DAI-050).

Manages six tables:
  normalized_categories, normalized_fields, normalized_values,
  normalized_test_fixtures, extraction_corrections,
  + ALTER template table to add normalize_mapping / required_normalized_fields

Reuses get_db_connection() / _return_conn() from templates_db.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import structlog

from templates_db import get_db_connection, _return_conn

log = structlog.get_logger()


# =============================================================================
# Schema Init
# =============================================================================

def init_normalization_db() -> None:
    """Creates all 6 normalization tables + indices if they don't exist yet."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS normalized_categories (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                parent_name TEXT REFERENCES normalized_categories(name),
                label_de TEXT NOT NULL,
                label_en TEXT NOT NULL,
                description TEXT NOT NULL,
                sort_order INT DEFAULT 100,
                active BOOLEAN DEFAULT true,
                created_at TIMESTAMPTZ DEFAULT now(),
                updated_at TIMESTAMPTZ DEFAULT now()
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_norm_categories_parent "
            "ON normalized_categories(parent_name)"
        )

        cur.execute("""
            CREATE TABLE IF NOT EXISTS normalized_fields (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                label_de TEXT NOT NULL,
                label_en TEXT NOT NULL,
                type TEXT NOT NULL,
                category TEXT NOT NULL REFERENCES normalized_categories(name),
                description TEXT NOT NULL,
                validation_rules JSONB,
                default_value TEXT,
                default_context JSONB,
                is_array BOOLEAN DEFAULT false,
                sort_order INT DEFAULT 100,
                active BOOLEAN DEFAULT true,
                created_at TIMESTAMPTZ DEFAULT now(),
                updated_at TIMESTAMPTZ DEFAULT now()
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_norm_fields_category "
            "ON normalized_fields(category)"
        )

        cur.execute("""
            CREATE TABLE IF NOT EXISTS normalized_values (
                id SERIAL PRIMARY KEY,
                field_name TEXT NOT NULL REFERENCES normalized_fields(name),
                canonical_value TEXT NOT NULL,
                aliases TEXT[] DEFAULT '{}',
                context JSONB,
                is_default BOOLEAN DEFAULT false,
                source TEXT NOT NULL DEFAULT 'system'
                    CHECK (source IN ('system', 'managed', 'user')),
                description TEXT,
                sort_order INT DEFAULT 100,
                active BOOLEAN DEFAULT true,
                created_at TIMESTAMPTZ DEFAULT now(),
                updated_at TIMESTAMPTZ DEFAULT now(),
                UNIQUE(field_name, canonical_value)
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_normalized_values_aliases "
            "ON normalized_values USING GIN (aliases)"
        )

        # Extend existing template table (idempotent)
        cur.execute("""
            DO $$ BEGIN
                ALTER TABLE template ADD COLUMN IF NOT EXISTS normalize_mapping JSONB;
                ALTER TABLE template
                    ADD COLUMN IF NOT EXISTS required_normalized_fields TEXT[] DEFAULT '{}';
            EXCEPTION WHEN others THEN NULL;
            END $$
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS normalized_test_fixtures (
                id SERIAL PRIMARY KEY,
                template_name TEXT NOT NULL,
                input_extracted JSONB NOT NULL,
                expected_normalized JSONB NOT NULL,
                description TEXT,
                active BOOLEAN DEFAULT true,
                created_at TIMESTAMPTZ DEFAULT now(),
                updated_at TIMESTAMPTZ DEFAULT now()
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_norm_fixtures_template "
            "ON normalized_test_fixtures(template_name)"
        )

        cur.execute("""
            CREATE TABLE IF NOT EXISTS extraction_corrections (
                id SERIAL PRIMARY KEY,
                document_id TEXT,
                template_name TEXT,
                field_name TEXT NOT NULL REFERENCES normalized_fields(name),
                old_value TEXT,
                new_value TEXT NOT NULL,
                source TEXT DEFAULT 'user',
                applied BOOLEAN DEFAULT false,
                reviewed_at TIMESTAMPTZ,
                reviewed_by TEXT,
                created_at TIMESTAMPTZ DEFAULT now()
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_corrections_field "
            "ON extraction_corrections(field_name)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_corrections_template "
            "ON extraction_corrections(template_name)"
        )

        conn.commit()

        # Seed normalization tables if empty
        cur.execute("SELECT COUNT(*) AS cnt FROM normalized_categories")
        row = cur.fetchone()
        if row["cnt"] == 0:
            seed_path = Path(__file__).parent / "seed_normalization.sql"
            if seed_path.exists():
                seed_sql = seed_path.read_text(encoding="utf-8")
                cur.execute(seed_sql)
                conn.commit()
                log.info("normalization_seeded", source=str(seed_path))

        log.info("normalization_db_initialized")
    finally:
        _return_conn(conn)


# =============================================================================
# Categories
# =============================================================================

def get_categories(active_only: bool = True) -> list[dict]:
    """Returns all categories, optionally filtered to active only."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        if active_only:
            cur.execute(
                "SELECT * FROM normalized_categories WHERE active = true "
                "ORDER BY sort_order, name"
            )
        else:
            cur.execute("SELECT * FROM normalized_categories ORDER BY sort_order, name")
        return [dict(r) for r in cur.fetchall()]
    finally:
        _return_conn(conn)


def create_category(
    name: str,
    label_de: str,
    label_en: str,
    description: str,
    parent_name: Optional[str] = None,
    sort_order: int = 100,
) -> dict:
    """Creates a new category. Returns the created row."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO normalized_categories "
            "(name, parent_name, label_de, label_en, description, sort_order) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING *",
            (name, parent_name, label_de, label_en, description, sort_order),
        )
        row = dict(cur.fetchone())
        conn.commit()
        return row
    finally:
        _return_conn(conn)


def update_category(name: str, **fields) -> Optional[dict]:
    """Updates allowed fields on a category. Returns updated row or None."""
    allowed = {"label_de", "label_en", "description", "sort_order", "active", "parent_name"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return None
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        set_clause = ", ".join(f"{k} = %s" for k in updates)
        set_clause += ", updated_at = now()"
        values = list(updates.values()) + [name]
        cur.execute(
            f"UPDATE normalized_categories SET {set_clause} WHERE name = %s RETURNING *",
            values,
        )
        row = cur.fetchone()
        conn.commit()
        return dict(row) if row else None
    finally:
        _return_conn(conn)


# =============================================================================
# Fields
# =============================================================================

def get_fields(category: Optional[str] = None, active_only: bool = True) -> list[dict]:
    """Returns all fields, optionally filtered by category."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        conditions = []
        params: list = []
        if active_only:
            conditions.append("active = true")
        if category:
            conditions.append("category = %s")
            params.append(category)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        cur.execute(
            f"SELECT * FROM normalized_fields {where} ORDER BY sort_order, name", params
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        _return_conn(conn)


def get_field(name: str) -> Optional[dict]:
    """Returns a single field by name, or None."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM normalized_fields WHERE name = %s", (name,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        _return_conn(conn)


def create_field(
    name: str,
    label_de: str,
    label_en: str,
    type: str,
    category: str,
    description: str,
    validation_rules: Optional[dict] = None,
    default_value: Optional[str] = None,
    default_context: Optional[dict] = None,
    is_array: bool = False,
    sort_order: int = 100,
) -> dict:
    """Creates a new normalized field."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO normalized_fields "
            "(name, label_de, label_en, type, category, description, "
            "validation_rules, default_value, default_context, is_array, sort_order) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
            (
                name, label_de, label_en, type, category, description,
                json.dumps(validation_rules) if validation_rules else None,
                default_value,
                json.dumps(default_context) if default_context else None,
                is_array, sort_order,
            ),
        )
        row = dict(cur.fetchone())
        conn.commit()
        return row
    finally:
        _return_conn(conn)


def update_field(name: str, **fields) -> Optional[dict]:
    """Updates allowed fields on a normalized_field row."""
    allowed = {
        "label_de", "label_en", "type", "category", "description",
        "validation_rules", "default_value", "default_context",
        "is_array", "sort_order", "active",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return None
    # Serialize JSONB columns
    for col in ("validation_rules", "default_context"):
        if col in updates and updates[col] is not None:
            updates[col] = json.dumps(updates[col])
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        set_clause = ", ".join(f"{k} = %s" for k in updates)
        set_clause += ", updated_at = now()"
        values = list(updates.values()) + [name]
        cur.execute(
            f"UPDATE normalized_fields SET {set_clause} WHERE name = %s RETURNING *",
            values,
        )
        row = cur.fetchone()
        conn.commit()
        return dict(row) if row else None
    finally:
        _return_conn(conn)


# =============================================================================
# Values
# =============================================================================

def get_values(field_name: str, active_only: bool = True) -> list[dict]:
    """Returns all canonical values for a field."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        if active_only:
            cur.execute(
                "SELECT * FROM normalized_values "
                "WHERE field_name = %s AND active = true ORDER BY sort_order, canonical_value",
                (field_name,),
            )
        else:
            cur.execute(
                "SELECT * FROM normalized_values WHERE field_name = %s "
                "ORDER BY sort_order, canonical_value",
                (field_name,),
            )
        return [dict(r) for r in cur.fetchall()]
    finally:
        _return_conn(conn)


def create_value(
    field_name: str,
    canonical_value: str,
    aliases: Optional[list[str]] = None,
    context: Optional[dict] = None,
    is_default: bool = False,
    source: str = "system",
    description: Optional[str] = None,
    sort_order: int = 100,
) -> dict:
    """Creates a new canonical value entry."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO normalized_values "
            "(field_name, canonical_value, aliases, context, is_default, source, "
            "description, sort_order) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
            (
                field_name, canonical_value,
                aliases or [],
                json.dumps(context) if context else None,
                is_default, source, description, sort_order,
            ),
        )
        row = dict(cur.fetchone())
        conn.commit()
        return row
    finally:
        _return_conn(conn)


def update_value(value_id: int, **fields) -> Optional[dict]:
    """Updates a normalized_value row by id."""
    allowed = {
        "canonical_value", "aliases", "context", "is_default",
        "source", "description", "sort_order", "active",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return None
    if "context" in updates and updates["context"] is not None:
        updates["context"] = json.dumps(updates["context"])
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        set_clause = ", ".join(f"{k} = %s" for k in updates)
        set_clause += ", updated_at = now()"
        values = list(updates.values()) + [value_id]
        cur.execute(
            f"UPDATE normalized_values SET {set_clause} WHERE id = %s RETURNING *",
            values,
        )
        row = cur.fetchone()
        conn.commit()
        return dict(row) if row else None
    finally:
        _return_conn(conn)


def find_canonical(field_name: str, raw_value: str) -> Optional[str]:
    """
    Looks up the canonical_value for a raw input string via:
    1. Exact match on canonical_value (case-insensitive)
    2. GIN-indexed alias array lookup (case-insensitive)
    Returns None if not found.
    """
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        # Try exact canonical match first
        cur.execute(
            "SELECT canonical_value FROM normalized_values "
            "WHERE field_name = %s AND active = true "
            "AND lower(canonical_value) = lower(%s) LIMIT 1",
            (field_name, raw_value),
        )
        row = cur.fetchone()
        if row:
            return row["canonical_value"]

        # Try alias match (GIN index on aliases array)
        cur.execute(
            "SELECT canonical_value FROM normalized_values "
            "WHERE field_name = %s AND active = true "
            "AND aliases @> ARRAY[%s]::TEXT[] LIMIT 1",
            (field_name, raw_value),
        )
        row = cur.fetchone()
        if row:
            return row["canonical_value"]

        # Case-insensitive alias fallback — array_to_string trick for ilike
        cur.execute(
            "SELECT canonical_value FROM normalized_values "
            "WHERE field_name = %s AND active = true "
            "AND EXISTS ("
            "  SELECT 1 FROM unnest(aliases) AS a WHERE lower(a) = lower(%s)"
            ") LIMIT 1",
            (field_name, raw_value),
        )
        row = cur.fetchone()
        return row["canonical_value"] if row else None
    finally:
        _return_conn(conn)


# =============================================================================
# Template Mappings
# =============================================================================

def get_mapping(template_name: str) -> Optional[dict]:
    """Returns the normalize_mapping JSONB for a template, or None."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT normalize_mapping, required_normalized_fields "
            "FROM template WHERE id = %s",
            (template_name,),
        )
        row = cur.fetchone()
        if not row:
            return None
        result: dict = {}
        if row["normalize_mapping"]:
            nm = row["normalize_mapping"]
            result["normalize_mapping"] = nm if isinstance(nm, dict) else json.loads(nm)
        if row["required_normalized_fields"]:
            result["required_normalized_fields"] = row["required_normalized_fields"]
        return result
    finally:
        _return_conn(conn)


def set_mapping(
    template_name: str,
    mapping: dict,
    required_fields: Optional[list[str]] = None,
) -> bool:
    """Upserts normalize_mapping (and optionally required_normalized_fields) on a template."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        if required_fields is not None:
            cur.execute(
                "UPDATE template SET normalize_mapping = %s, "
                "required_normalized_fields = %s WHERE id = %s",
                (json.dumps(mapping), required_fields, template_name),
            )
        else:
            cur.execute(
                "UPDATE template SET normalize_mapping = %s WHERE id = %s",
                (json.dumps(mapping), template_name),
            )
        updated = cur.rowcount > 0
        conn.commit()
        return updated
    finally:
        _return_conn(conn)


# =============================================================================
# Test Fixtures
# =============================================================================

def get_fixtures(template_name: str, active_only: bool = True) -> list[dict]:
    """Returns all test fixtures for a template."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        if active_only:
            cur.execute(
                "SELECT * FROM normalized_test_fixtures "
                "WHERE template_name = %s AND active = true ORDER BY id",
                (template_name,),
            )
        else:
            cur.execute(
                "SELECT * FROM normalized_test_fixtures "
                "WHERE template_name = %s ORDER BY id",
                (template_name,),
            )
        return [dict(r) for r in cur.fetchall()]
    finally:
        _return_conn(conn)


def create_fixture(
    template_name: str,
    input_extracted: dict,
    expected_normalized: dict,
    description: Optional[str] = None,
) -> dict:
    """Creates a test fixture for a template."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO normalized_test_fixtures "
            "(template_name, input_extracted, expected_normalized, description) "
            "VALUES (%s, %s, %s, %s) RETURNING *",
            (
                template_name,
                json.dumps(input_extracted),
                json.dumps(expected_normalized),
                description,
            ),
        )
        row = dict(cur.fetchone())
        conn.commit()
        return row
    finally:
        _return_conn(conn)


def run_fixture(fixture_id: int) -> dict:
    """
    Runs a test fixture: for each key in expected_normalized, calls find_canonical
    on the corresponding value from input_extracted.
    Returns {"fixture_id": ..., "passed": bool, "results": [...]}.
    """
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM normalized_test_fixtures WHERE id = %s", (fixture_id,)
        )
        row = cur.fetchone()
    finally:
        _return_conn(conn)

    if not row:
        return {"fixture_id": fixture_id, "passed": False, "error": "fixture not found"}

    fixture = dict(row)
    input_extracted = fixture["input_extracted"]
    expected_normalized = fixture["expected_normalized"]
    if isinstance(input_extracted, str):
        input_extracted = json.loads(input_extracted)
    if isinstance(expected_normalized, str):
        expected_normalized = json.loads(expected_normalized)

    results = []
    all_passed = True
    for field_name, expected_val in expected_normalized.items():
        raw = input_extracted.get(field_name)
        canonical = find_canonical(field_name, str(raw)) if raw is not None else None
        passed = canonical == expected_val
        if not passed:
            all_passed = False
        results.append({
            "field": field_name,
            "raw": raw,
            "canonical": canonical,
            "expected": expected_val,
            "passed": passed,
        })

    return {"fixture_id": fixture_id, "passed": all_passed, "results": results}


# =============================================================================
# Extraction Corrections
# =============================================================================

def get_corrections(
    template_name: Optional[str] = None,
    field_name: Optional[str] = None,
    applied: Optional[bool] = None,
) -> list[dict]:
    """Returns correction entries, optionally filtered."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        conditions = []
        params: list = []
        if template_name:
            conditions.append("template_name = %s")
            params.append(template_name)
        if field_name:
            conditions.append("field_name = %s")
            params.append(field_name)
        if applied is not None:
            conditions.append("applied = %s")
            params.append(applied)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        cur.execute(
            f"SELECT * FROM extraction_corrections {where} ORDER BY created_at DESC",
            params,
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        _return_conn(conn)


def create_correction(
    field_name: str,
    new_value: str,
    document_id: Optional[str] = None,
    template_name: Optional[str] = None,
    old_value: Optional[str] = None,
    source: str = "user",
) -> dict:
    """Records an extraction correction."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO extraction_corrections "
            "(document_id, template_name, field_name, old_value, new_value, source) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING *",
            (document_id, template_name, field_name, old_value, new_value, source),
        )
        row = dict(cur.fetchone())
        conn.commit()
        return row
    finally:
        _return_conn(conn)


def review_correction(
    correction_id: int,
    reviewed_by: str,
    applied: bool = True,
) -> Optional[dict]:
    """Marks a correction as reviewed (and optionally applied)."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE extraction_corrections "
            "SET applied = %s, reviewed_by = %s, reviewed_at = now() "
            "WHERE id = %s RETURNING *",
            (applied, reviewed_by, correction_id),
        )
        row = cur.fetchone()
        conn.commit()
        return dict(row) if row else None
    finally:
        _return_conn(conn)
