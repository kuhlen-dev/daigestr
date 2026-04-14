"""
Tests für normalizer_db.py (T-DAI-050).

Requires a live PostgreSQL at DATABASE_URL (default: postgresql://daigestr:daigestr@localhost:15432/daigestr).
Run: DATABASE_URL=postgresql://daigestr:daigestr@localhost:15432/daigestr python3 -m pytest tests/test_normalizer_db.py -v --tb=short
"""

import os
import sys
import json
import pytest

# Ensure mcp/ is on the path when running from repo root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Skip all tests if no real DATABASE_URL is configured for test run
pytestmark = pytest.mark.skipif(
    "localhost" not in os.getenv("DATABASE_URL", ""),
    reason="Requires local PostgreSQL (DATABASE_URL must contain 'localhost')",
)


@pytest.fixture(scope="module", autouse=True)
def setup_db():
    """Initialize both template and normalization DB tables."""
    from templates_db import init_templates_db, pool_reset
    from normalizer_db import init_normalization_db

    pool_reset()
    init_templates_db()
    init_normalization_db()
    yield
    pool_reset()


# ---------------------------------------------------------------------------
# Table existence
# ---------------------------------------------------------------------------

def test_tables_exist():
    """All six normalization-related tables must exist after init."""
    from templates_db import get_db_connection, _return_conn

    expected = {
        "normalized_categories",
        "normalized_fields",
        "normalized_values",
        "normalized_test_fixtures",
        "extraction_corrections",
    }
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        )
        tables = {r["tablename"] for r in cur.fetchall()}
    finally:
        _return_conn(conn)

    for t in expected:
        assert t in tables, f"Table missing: {t}"


def test_template_columns_added():
    """template table must have normalize_mapping and required_normalized_fields columns."""
    from templates_db import get_db_connection, _return_conn

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'template' AND column_name IN "
            "('normalize_mapping', 'required_normalized_fields')"
        )
        cols = {r["column_name"] for r in cur.fetchall()}
    finally:
        _return_conn(conn)

    assert "normalize_mapping" in cols
    assert "required_normalized_fields" in cols


# ---------------------------------------------------------------------------
# Categories CRUD
# ---------------------------------------------------------------------------

@pytest.fixture
def category(setup_db):
    from normalizer_db import create_category, get_categories
    from templates_db import get_db_connection, _return_conn

    # Clean up any leftover fixture data
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM normalized_categories WHERE name = 'test_addr'"
        )
        conn.commit()
    finally:
        _return_conn(conn)

    cat = create_category(
        name="test_addr",
        label_de="Adresse",
        label_en="Address",
        description="Postal address fields",
        sort_order=10,
    )
    yield cat

    # Teardown: remove dependent rows first
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        # Remove fields referencing this category (cascade-like cleanup)
        cur.execute(
            "DELETE FROM normalized_fields WHERE category = 'test_addr'"
        )
        cur.execute("DELETE FROM normalized_categories WHERE name = 'test_addr'")
        conn.commit()
    finally:
        _return_conn(conn)


def test_create_category(category):
    assert category["name"] == "test_addr"
    assert category["label_de"] == "Adresse"
    assert category["active"] is True


def test_get_categories(category):
    from normalizer_db import get_categories

    cats = get_categories()
    names = [c["name"] for c in cats]
    assert "test_addr" in names


def test_update_category(category):
    from normalizer_db import update_category

    updated = update_category("test_addr", label_en="Postal Address", sort_order=5)
    assert updated is not None
    assert updated["label_en"] == "Postal Address"
    assert updated["sort_order"] == 5


# ---------------------------------------------------------------------------
# Fields CRUD
# ---------------------------------------------------------------------------

@pytest.fixture
def field(category):
    from normalizer_db import create_field
    from templates_db import get_db_connection, _return_conn

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM normalized_fields WHERE name = 'test_country'")
        conn.commit()
    finally:
        _return_conn(conn)

    f = create_field(
        name="test_country",
        label_de="Land",
        label_en="Country",
        type="string",
        category="test_addr",
        description="ISO 3166-1 alpha-2 country code",
        validation_rules={"pattern": "^[A-Z]{2}$"},
        sort_order=1,
    )
    yield f

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        # Remove dependent values and corrections first
        cur.execute("DELETE FROM normalized_values WHERE field_name = 'test_country'")
        cur.execute(
            "DELETE FROM extraction_corrections WHERE field_name = 'test_country'"
        )
        cur.execute("DELETE FROM normalized_fields WHERE name = 'test_country'")
        conn.commit()
    finally:
        _return_conn(conn)


def test_create_field(field):
    assert field["name"] == "test_country"
    assert field["type"] == "string"


def test_get_field(field):
    from normalizer_db import get_field

    f = get_field("test_country")
    assert f is not None
    assert f["category"] == "test_addr"


def test_get_fields_by_category(field):
    from normalizer_db import get_fields

    fields = get_fields(category="test_addr")
    names = [f["name"] for f in fields]
    assert "test_country" in names


def test_update_field(field):
    from normalizer_db import update_field

    updated = update_field("test_country", description="Updated description")
    assert updated is not None
    assert updated["description"] == "Updated description"


# ---------------------------------------------------------------------------
# Values CRUD + find_canonical
# ---------------------------------------------------------------------------

@pytest.fixture
def value(field):
    from normalizer_db import create_value
    from templates_db import get_db_connection, _return_conn

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM normalized_values "
            "WHERE field_name = 'test_country' AND canonical_value = 'DE'"
        )
        conn.commit()
    finally:
        _return_conn(conn)

    v = create_value(
        field_name="test_country",
        canonical_value="DE",
        aliases=["Deutschland", "Germany", "de", "germany"],
        is_default=True,
        source="system",
        description="Germany",
    )
    yield v


def test_create_value(value):
    assert value["canonical_value"] == "DE"
    assert value["is_default"] is True


def test_get_values(value):
    from normalizer_db import get_values

    vals = get_values("test_country")
    canonicals = [v["canonical_value"] for v in vals]
    assert "DE" in canonicals


def test_update_value(value):
    from normalizer_db import update_value

    updated = update_value(value["id"], description="Federal Republic of Germany")
    assert updated is not None
    assert updated["description"] == "Federal Republic of Germany"


def test_find_canonical_exact(value):
    from normalizer_db import find_canonical

    result = find_canonical("test_country", "DE")
    assert result == "DE"


def test_find_canonical_alias_exact(value):
    from normalizer_db import find_canonical

    result = find_canonical("test_country", "Deutschland")
    assert result == "DE"


def test_find_canonical_alias_case_insensitive(value):
    from normalizer_db import find_canonical

    result = find_canonical("test_country", "germany")
    assert result == "DE"


def test_find_canonical_not_found(value):
    from normalizer_db import find_canonical

    result = find_canonical("test_country", "XX_DOES_NOT_EXIST")
    assert result is None


def test_gin_index_alias_lookup(value):
    """Verify GIN index is used by checking alias array lookup works correctly."""
    from normalizer_db import find_canonical

    # "de" is a lowercase alias — must resolve via GIN or case-insensitive fallback
    result = find_canonical("test_country", "de")
    assert result == "DE"


# ---------------------------------------------------------------------------
# Template Mappings
# ---------------------------------------------------------------------------

def test_set_and_get_mapping(setup_db):
    """set_mapping / get_mapping round-trip on template table."""
    from normalizer_db import set_mapping, get_mapping
    from templates_db import get_db_connection, _return_conn

    # We need a template row to attach a mapping to.
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO template (id, category, display_name, schema) "
            "VALUES ('_test_norm_tpl', 'other', 'Test Norm Template', '{}') "
            "ON CONFLICT (id) DO NOTHING"
        )
        conn.commit()
    finally:
        _return_conn(conn)

    mapping = {"country": "test_country", "amount": "invoice_amount"}
    result = set_mapping("_test_norm_tpl", mapping, required_fields=["country"])
    assert result is True

    fetched = get_mapping("_test_norm_tpl")
    assert fetched is not None
    assert fetched["normalize_mapping"]["country"] == "test_country"
    assert "country" in fetched["required_normalized_fields"]

    # Cleanup
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM template WHERE id = '_test_norm_tpl'")
        conn.commit()
    finally:
        _return_conn(conn)


def test_get_mapping_nonexistent(setup_db):
    from normalizer_db import get_mapping

    result = get_mapping("__no_such_template__")
    assert result is None


# ---------------------------------------------------------------------------
# Test Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fixture_row(field, value):
    from normalizer_db import create_fixture
    from templates_db import get_db_connection, _return_conn

    fix = create_fixture(
        template_name="test_invoice",
        input_extracted={"test_country": "Deutschland"},
        expected_normalized={"test_country": "DE"},
        description="Germany alias resolution",
    )
    yield fix

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM normalized_test_fixtures WHERE id = %s", (fix["id"],))
        conn.commit()
    finally:
        _return_conn(conn)


def test_create_fixture(fixture_row):
    assert fixture_row["template_name"] == "test_invoice"
    assert fixture_row["description"] == "Germany alias resolution"


def test_get_fixtures(fixture_row):
    from normalizer_db import get_fixtures

    fixtures = get_fixtures("test_invoice")
    ids = [f["id"] for f in fixtures]
    assert fixture_row["id"] in ids


def test_run_fixture_passes(fixture_row):
    from normalizer_db import run_fixture

    result = run_fixture(fixture_row["id"])
    assert result["passed"] is True
    assert len(result["results"]) == 1
    assert result["results"][0]["canonical"] == "DE"


def test_run_fixture_not_found():
    from normalizer_db import run_fixture

    result = run_fixture(999999999)
    assert result["passed"] is False
    assert "error" in result


# ---------------------------------------------------------------------------
# Extraction Corrections
# ---------------------------------------------------------------------------

@pytest.fixture
def correction(field, value):
    from normalizer_db import create_correction
    from templates_db import get_db_connection, _return_conn

    c = create_correction(
        field_name="test_country",
        new_value="AT",
        old_value="Austria",
        document_id="doc-001",
        template_name="test_invoice",
        source="user",
    )
    yield c

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM extraction_corrections WHERE id = %s", (c["id"],))
        conn.commit()
    finally:
        _return_conn(conn)


def test_create_correction(correction):
    assert correction["field_name"] == "test_country"
    assert correction["new_value"] == "AT"
    assert correction["applied"] is False


def test_get_corrections(correction):
    from normalizer_db import get_corrections

    corrections = get_corrections(field_name="test_country")
    ids = [c["id"] for c in corrections]
    assert correction["id"] in ids


def test_get_corrections_by_template(correction):
    from normalizer_db import get_corrections

    corrections = get_corrections(template_name="test_invoice")
    assert len(corrections) >= 1


def test_review_correction(correction):
    from normalizer_db import review_correction, get_corrections

    reviewed = review_correction(correction["id"], reviewed_by="test_user", applied=True)
    assert reviewed is not None
    assert reviewed["applied"] is True
    assert reviewed["reviewed_by"] == "test_user"
    assert reviewed["reviewed_at"] is not None


def test_get_corrections_applied_filter(correction):
    from normalizer_db import get_corrections, review_correction

    review_correction(correction["id"], reviewed_by="test_user", applied=True)
    applied = get_corrections(field_name="test_country", applied=True)
    ids = [c["id"] for c in applied]
    assert correction["id"] in ids

    not_applied = get_corrections(field_name="test_country", applied=False)
    ids_na = [c["id"] for c in not_applied]
    assert correction["id"] not in ids_na


def test_init_normalization_db_repairs_mapping_drift():
    """Existing DBs with cleared normalize_mapping columns must be repaired."""
    from normalizer_db import init_normalization_db, get_mapping
    from templates_db import get_db_connection, _return_conn
    import normalizer_cache

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE template SET normalize_mapping = NULL, required_normalized_fields = '{}' "
            "WHERE id IN ('bank_statement', 'invoice', 'telecom_bill')"
        )
        conn.commit()
    finally:
        _return_conn(conn)

    normalizer_cache.cache_reset()
    init_normalization_db()
    normalizer_cache.cache_reset()

    for template_name in ("bank_statement", "invoice", "telecom_bill"):
        mapping = get_mapping(template_name)
        assert mapping is not None, f"Expected mapping metadata for {template_name}"
        assert mapping["normalize_mapping"], f"Expected normalize_mapping for {template_name}"
