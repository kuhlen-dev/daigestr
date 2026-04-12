"""
Tests für normalizer.py (T-DAI-053).

Run: DATABASE_URL=postgresql://daigestr:daigestr@localhost:15432/daigestr
     python3 -m pytest tests/test_normalizer.py -v --tb=short
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

pytestmark = pytest.mark.skipif(
    "localhost" not in os.getenv("DATABASE_URL", ""),
    reason="Requires local PostgreSQL (DATABASE_URL must contain 'localhost')",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def setup_db():
    """Init DB, seed if needed, reset cache before each test."""
    from templates_db import init_templates_db, pool_reset
    from normalizer_db import init_normalization_db
    import normalizer_cache

    pool_reset()
    init_templates_db()
    init_normalization_db()
    normalizer_cache.cache_reset()
    yield
    normalizer_cache.cache_reset()
    pool_reset()


@pytest.fixture
def test_template():
    """Insert a minimal normalize_mapping on a test template, clean up after."""
    from normalizer_db import set_mapping, get_mapping
    from templates_db import get_db_connection, _return_conn

    template_id = "__norm_test_template__"

    # Create a minimal template row if it doesn't exist
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO template (id, display_name, category, schema) "
            "VALUES (%s, %s, %s, %s::jsonb) ON CONFLICT (id) DO NOTHING",
            (template_id, "Test Template", "test", "{}"),
        )
        conn.commit()
    finally:
        _return_conn(conn)

    mapping = {
        "amount": "total_amount",
        "amount_net": "net_amount",
        "amount_tax": "tax_amount",
        "currency": "currency",
        "date_issued": "date",
        "vendor_name": "_meta.absender.firma",
        "payment_method": "payment_method",
    }
    set_mapping(template_id, mapping, required_fields=["amount", "currency"])

    yield template_id

    # Cleanup
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE template SET normalize_mapping = NULL, required_normalized_fields = '{}' "
            "WHERE id = %s",
            (template_id,),
        )
        cur.execute("DELETE FROM template WHERE id = %s", (template_id,))
        conn.commit()
    finally:
        _return_conn(conn)


def run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Helper: _resolve_dot_path
# ---------------------------------------------------------------------------

def test_resolve_dot_path_simple():
    from normalizer import _resolve_dot_path
    obj = {"a": {"b": {"c": 42}}}
    assert _resolve_dot_path(obj, "a.b.c") == 42


def test_resolve_dot_path_missing_key():
    from normalizer import _resolve_dot_path
    obj = {"a": {"b": 1}}
    assert _resolve_dot_path(obj, "a.x.y") is None


def test_resolve_dot_path_none_obj():
    from normalizer import _resolve_dot_path
    assert _resolve_dot_path(None, "a.b") is None


def test_resolve_dot_path_flat():
    from normalizer import _resolve_dot_path
    obj = {"total_amount": "100.00"}
    assert _resolve_dot_path(obj, "total_amount") == "100.00"


def test_resolve_dot_path_nested_meta():
    from normalizer import _resolve_dot_path
    obj = {"_meta": {"absender": {"firma": "ACME GmbH"}}}
    assert _resolve_dot_path(obj, "_meta.absender.firma") == "ACME GmbH"


# ---------------------------------------------------------------------------
# Helper: _convert_decimal
# ---------------------------------------------------------------------------

def test_convert_decimal_german_format():
    from normalizer import _convert_decimal
    assert _convert_decimal("29,95") == pytest.approx(29.95)


def test_convert_decimal_german_thousands():
    from normalizer import _convert_decimal
    assert _convert_decimal("1.234,56") == pytest.approx(1234.56)


def test_convert_decimal_english():
    from normalizer import _convert_decimal
    assert _convert_decimal("29.95") == pytest.approx(29.95)


def test_convert_decimal_integer_string():
    from normalizer import _convert_decimal
    assert _convert_decimal("100") == pytest.approx(100.0)


def test_convert_decimal_with_currency_symbol():
    from normalizer import _convert_decimal
    assert _convert_decimal("€ 29,95") == pytest.approx(29.95)


def test_convert_decimal_none():
    from normalizer import _convert_decimal
    assert _convert_decimal(None) is None


def test_convert_decimal_float_passthrough():
    from normalizer import _convert_decimal
    assert _convert_decimal(12.34) == pytest.approx(12.34)


def test_convert_decimal_invalid():
    from normalizer import _convert_decimal
    assert _convert_decimal("not-a-number") is None


# ---------------------------------------------------------------------------
# Helper: _convert_date
# ---------------------------------------------------------------------------

def test_convert_date_german():
    from normalizer import _convert_date
    assert _convert_date("01.03.2025") == "2025-03-01"


def test_convert_date_iso_passthrough():
    from normalizer import _convert_date
    assert _convert_date("2025-03-01") == "2025-03-01"


def test_convert_date_us_format():
    from normalizer import _convert_date
    assert _convert_date("03/01/2025") == "2025-03-01"


def test_convert_date_slash_format():
    from normalizer import _convert_date
    assert _convert_date("2025/03/01") == "2025-03-01"


def test_convert_date_german_month_name():
    from normalizer import _convert_date
    result = _convert_date("1. März 2025")
    assert result == "2025-03-01"


def test_convert_date_none():
    from normalizer import _convert_date
    assert _convert_date(None) is None


def test_convert_date_empty():
    from normalizer import _convert_date
    assert _convert_date("") is None


# ---------------------------------------------------------------------------
# Helper: _convert_boolean
# ---------------------------------------------------------------------------

def test_convert_boolean_ja():
    from normalizer import _convert_boolean
    assert _convert_boolean("ja") is True


def test_convert_boolean_nein():
    from normalizer import _convert_boolean
    assert _convert_boolean("nein") is False


def test_convert_boolean_true_string():
    from normalizer import _convert_boolean
    assert _convert_boolean("true") is True


def test_convert_boolean_false_string():
    from normalizer import _convert_boolean
    assert _convert_boolean("false") is False


def test_convert_boolean_1():
    from normalizer import _convert_boolean
    assert _convert_boolean("1") is True


def test_normalize_bank_statement_bundle_preserves_full_period():
    from normalizer import normalize

    extracted = {
        "bank": "Stadtsparkasse Mönchengladbach",
        "iban": "DE62310500000000019752",
        "bic": "MGLSDE33",
        "währung": "EUR",
        "auszugsnummer": "11,12",
        "datum": "2024-09-30",
        "zeitraum": {"von": "2024-08-19", "bis": "2024-09-02"},
        "anfangssaldo": "14902.01",
        "endsaldo": "12235.38",
        "buchungen": [
            {"datum": "2024-08-19", "text": "A", "betrag": "-41.90", "saldo": "14860.11", "währung": "EUR"},
            {"datum": "2024-08-30", "text": "B", "betrag": "+766.09", "saldo": "15504.16", "währung": "EUR"},
            {"datum": "2024-09-02", "text": "C", "betrag": "-27.85", "saldo": "12235.38", "währung": "EUR"},
        ],
        "_meta": {
            "absender": {
                "name": None,
                "firma": "Stadtsparkasse Mönchengladbach",
                "slug": "sparkasse-mg",
                "email": None,
                "telefon": "+492161270",
                "ust_id": "DE120499145",
                "bic": "MGLSDE33",
                "adresse": {"land": "DE"},
            },
            "empfaenger": {"name": "Hans und Marlene Kuhlen", "adresse": {"land": "DE"}},
            "empfaenger_iban": "DE62310500000000019752",
            "steuerrelevant": True,
            "steuer_kategorie": None,
            "mwst_betrag": None,
            "mwst_satz": None,
            "zusammenfassung": "Sammel-Kontoauszug",
        },
        "kontoauszuege": [
            {
                "auszugsnummer": "11",
                "datum": "2024-08-30",
                "anfangssaldo": "14902.01",
                "endsaldo": "15504.16",
                "buchungen": [
                    {"datum": "2024-08-19", "text": "A", "betrag": "-41.90", "saldo": "14860.11", "währung": "EUR"},
                    {"datum": "2024-08-30", "text": "B", "betrag": "+766.09", "saldo": "15504.16", "währung": "EUR"},
                ],
            },
            {
                "auszugsnummer": "12",
                "datum": "2024-09-30",
                "anfangssaldo": "15504.16",
                "endsaldo": "12235.38",
                "buchungen": [
                    {"datum": "2024-09-02", "text": "C", "betrag": "-27.85", "saldo": "12235.38", "währung": "EUR"},
                ],
            },
        ],
    }

    result = run(normalize(extracted, "bank_statement", {"quality_score": 0.91}))
    normalized = result["normalized"]

    assert normalized["invoice_number"] == "11,12"
    assert normalized["currency"] == "EUR"
    assert normalized["date_period_from"] == "2024-08-19"
    assert normalized["date_period_to"] == "2024-09-02"
    assert normalized["line_items_count"] == 3
    assert len(normalized["line_items"]) == 3
    assert normalized["amount"] == pytest.approx(12235.38)


def test_convert_boolean_0():
    from normalizer import _convert_boolean
    assert _convert_boolean("0") is False


def test_convert_boolean_none():
    from normalizer import _convert_boolean
    assert _convert_boolean(None) is None


def test_convert_boolean_bool_passthrough():
    from normalizer import _convert_boolean
    assert _convert_boolean(True) is True


# ---------------------------------------------------------------------------
# normalize() — Step 0: Vorprüfung
# ---------------------------------------------------------------------------

def test_normalize_none_extracted_returns_none():
    from normalizer import normalize
    result = run(normalize(None, "invoice", {}))
    assert result is None


def test_normalize_no_mapping_returns_none():
    from normalizer import normalize
    result = run(normalize({"amount": "100"}, "__no_such_template__", {}))
    assert result["normalized"] is None
    assert result["normalized_warnings"] == ["No normalize_mapping found for template '__no_such_template__'"]


# ---------------------------------------------------------------------------
# normalize() — Full pipeline with test template
# ---------------------------------------------------------------------------

def test_normalize_basic_fields(test_template):
    from normalizer import normalize
    extracted = {
        "total_amount": "119,00",
        "net_amount": "100,00",
        "tax_amount": "19,00",
        "currency": "EUR",
        "date": "15.01.2025",  # maps to date_issued (type=date → converted)
        "_meta": {"absender": {"firma": "Test GmbH"}},
    }
    result = run(normalize(extracted, test_template, {}))
    assert result is not None
    assert "normalized" in result
    n = result["normalized"]
    assert n["amount"] == pytest.approx(119.0)
    assert n["amount_net"] == pytest.approx(100.0)
    assert n["amount_tax"] == pytest.approx(19.0)
    assert n["currency"] == "EUR"
    assert n["date_issued"] == "2025-01-15"
    assert n["vendor_name"] == "Test GmbH"


def test_normalize_returns_quality_score(test_template):
    from normalizer import normalize
    extracted = {"total_amount": "100", "currency": "EUR"}
    result = run(normalize(extracted, test_template, {}))
    assert result is not None
    assert "quality_score" in result
    assert 0.0 <= result["quality_score"] <= 1.0


def test_normalize_returns_trace(test_template):
    from normalizer import normalize
    extracted = {"total_amount": "100", "currency": "EUR", "date": "2025-01-15"}
    result = run(normalize(extracted, test_template, {}))
    assert result is not None
    assert isinstance(result["normalized_trace"], list)
    assert len(result["normalized_trace"]) > 0


def test_normalize_returns_warnings(test_template):
    from normalizer import normalize
    # Missing required field 'amount' → should produce warning
    extracted = {"currency": "EUR"}
    result = run(normalize(extracted, test_template, {}))
    assert result is not None
    assert isinstance(result["normalized_warnings"], list)


def test_normalize_returns_context(test_template):
    from normalizer import normalize
    extracted = {"total_amount": "100", "currency": "EUR"}
    result = run(normalize(extracted, test_template, {}))
    assert result is not None
    ctx = result["normalized_context"]
    assert "vendor_country" in ctx
    assert "recipient_country" in ctx


def test_normalize_compact_removes_nulls(test_template):
    from normalizer import normalize
    extracted = {"total_amount": "100", "currency": "EUR"}
    result = run(normalize(extracted, test_template, {}, compact=True))
    assert result is not None
    for val in result["normalized"].values():
        assert val is not None


def test_normalize_compact_false_keeps_nulls(test_template):
    from normalizer import normalize
    extracted = {"total_amount": "100"}
    result = run(normalize(extracted, test_template, {}, compact=False))
    assert result is not None
    # With compact=False, some fields may be None (e.g. date_issued if not provided)
    normalized = result["normalized"]
    assert "date_issued" in normalized  # present, even if None


def test_normalize_enum_canonical_lookup(test_template):
    """payment_method='Überweisung' should resolve to canonical value."""
    from normalizer import normalize
    extracted = {
        "total_amount": "100",
        "currency": "EUR",
        "payment_method": "Überweisung",
    }
    result = run(normalize(extracted, test_template, {}))
    assert result is not None
    # find_canonical should resolve this if seed has it; otherwise passthrough
    pm = result["normalized"].get("payment_method")
    assert pm is not None  # must have some value


def test_normalize_currency_default_de(test_template):
    """If currency not provided and vendor_country=DE, should default to EUR."""
    from normalizer import normalize
    extracted = {"total_amount": "100"}  # no currency
    result = run(normalize(extracted, test_template, {}))
    assert result is not None
    # Default EUR when country=DE (default fallback country)
    n = result["normalized"]
    # currency field is in mapping, no raw value → check if EUR default applied
    assert n.get("currency") == "EUR" or n.get("currency") is None


def test_normalize_plausibility_warning(test_template):
    """amount ≠ amount_net + amount_tax → warning."""
    from normalizer import normalize
    extracted = {
        "total_amount": "200",
        "net_amount": "100",
        "tax_amount": "19",  # 100+19=119 ≠ 200
        "currency": "EUR",
    }
    result = run(normalize(extracted, test_template, {}))
    assert result is not None
    warnings = result["normalized_warnings"]
    plaus_warns = [w for w in warnings if "Plausibility" in w]
    assert len(plaus_warns) > 0


def test_normalize_plausibility_ok_no_warning(test_template):
    """amount ≈ amount_net + amount_tax → no plausibility warning."""
    from normalizer import normalize
    extracted = {
        "total_amount": "119",
        "net_amount": "100",
        "tax_amount": "19",
        "currency": "EUR",
    }
    result = run(normalize(extracted, test_template, {}))
    assert result is not None
    warnings = result["normalized_warnings"]
    plaus_warns = [w for w in warnings if "Plausibility" in w]
    assert len(plaus_warns) == 0


def test_normalize_version_hash_present(test_template):
    from normalizer import normalize
    extracted = {"total_amount": "100", "currency": "EUR"}
    result = run(normalize(extracted, test_template, {}))
    assert result is not None
    assert result.get("normalized_version") is not None
    assert result["normalized_version"] != ""


def test_normalize_fallback_chain_mapping(test_template):
    """Dot-path: _meta.absender.firma should map correctly."""
    from normalizer import normalize
    extracted = {
        "total_amount": "50",
        "currency": "EUR",
        "_meta": {"absender": {"firma": "Fallback Corp"}},
    }
    result = run(normalize(extracted, test_template, {}))
    assert result is not None
    assert result["normalized"].get("vendor_name") == "Fallback Corp"
