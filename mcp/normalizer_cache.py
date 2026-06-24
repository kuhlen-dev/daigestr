"""
Normalizer Cache — In-Memory Cache für Normalization-Daten (T-DAI-054).

Cached: fields, values (per field), mappings (per template), categories.
Version-Hash: SHA256 von max(updated_at) aus normalized_fields + normalized_values + template.normalize_mapping.
TTL: NORMALIZE_CACHE_TTL_SECONDS — nach Ablauf wird Hash gegen DB geprüft, bei Änderung Cache geleert.
"""

import hashlib
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import structlog

from settings import NORMALIZE_CACHE_TTL_SECONDS, NORMALIZE_CACHE_ENABLED
from templates_db import get_db_connection, _return_conn

log = structlog.get_logger()

# =============================================================================
# Cache State
# =============================================================================

_cache: dict = {
    "fields": None,       # list[dict] — alle normalized_fields
    "values": {},         # dict[field_name, list[dict]]
    "mappings": {},       # dict[template_name, dict | None]
    "categories": None,   # list[dict]
}

_meta: dict = {
    "loaded_at": 0.0,        # Zeitstempel des letzten vollständigen Loads
    "version_hash": None,    # SHA256-Hash der DB-Versionen
    "last_check": 0.0,       # Zeitstempel des letzten Hash-Checks
}

_cache_lock = threading.Lock()


# =============================================================================
# Internal Helpers
# =============================================================================

def _compute_version_hash() -> str:
    """
    Berechnet SHA256 von max(updated_at) aus:
    - normalized_fields
    - normalized_values
    - template.normalize_mapping (WHERE normalize_mapping IS NOT NULL)
    """
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT GREATEST("
            "  (SELECT COALESCE(MAX(updated_at), '1970-01-01'::timestamptz) FROM normalized_fields),"
            "  (SELECT COALESCE(MAX(updated_at), '1970-01-01'::timestamptz) FROM normalized_values),"
            "  (SELECT COALESCE(MAX(updated_at), '1970-01-01'::timestamptz) FROM normalized_categories),"
            "  (SELECT COALESCE(MAX(updated_at), '1970-01-01'::timestamptz)"
            "   FROM template WHERE normalize_mapping IS NOT NULL)"
            ") AS version_ts"
        )
        row = cur.fetchone()
        ts = row["version_ts"] if row and row["version_ts"] else "none"
        raw = str(ts).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()
    finally:
        _return_conn(conn)


def _is_stale() -> bool:
    """Gibt True zurück wenn der Cache abgelaufen ist (TTL überschritten)."""
    return (time.monotonic() - _meta["last_check"]) >= NORMALIZE_CACHE_TTL_SECONDS


def _check_and_invalidate() -> None:
    """Prüft ob sich der Hash geändert hat. Wenn ja, leert den Cache."""
    if not NORMALIZE_CACHE_ENABLED:
        return
    with _cache_lock:
        if not _is_stale():
            return
        try:
            new_hash = _compute_version_hash()
            _meta["last_check"] = time.monotonic()
            if new_hash != _meta["version_hash"]:
                log.info(
                    "normalizer_cache_invalidated",
                    old_hash=_meta["version_hash"],
                    new_hash=new_hash,
                )
                _invalidate_all()
                _meta["version_hash"] = new_hash
        except Exception as e:
            log.warning("normalizer_cache_hash_check_failed", error=str(e))


def _invalidate_all() -> None:
    """Leert alle Cache-Einträge (aber nicht den Hash selbst)."""
    _cache["fields"] = None
    _cache["values"] = {}
    _cache["mappings"] = {}
    _cache["categories"] = None
    _meta["loaded_at"] = 0.0


# =============================================================================
# Public API
# =============================================================================

def cache_reset() -> None:
    """Leert den gesamten Cache inklusive Hash-Tracking — für Tests."""
    _invalidate_all()
    _meta["version_hash"] = None
    _meta["last_check"] = 0.0
    log.debug("normalizer_cache_reset")


def invalidate_cache() -> None:
    """Erzwingt Cache-Invalidierung beim nächsten Zugriff."""
    _invalidate_all()
    _meta["version_hash"] = None
    _meta["last_check"] = 0.0
    log.info("normalizer_cache_invalidated_manual")


def get_cached_fields() -> list[dict]:
    """
    Gibt alle normalized_fields zurück (aus Cache oder DB).
    Prüft bei jeder Anfrage ob TTL abgelaufen ist und invalidiert bei Bedarf.
    """
    if not NORMALIZE_CACHE_ENABLED:
        from normalizer_db import get_fields
        return get_fields()

    _check_and_invalidate()

    if _cache["fields"] is None:
        from normalizer_db import get_fields
        _cache["fields"] = get_fields()
        _meta["loaded_at"] = time.monotonic()
        if _meta["version_hash"] is None:
            _meta["version_hash"] = _compute_version_hash()
            _meta["last_check"] = time.monotonic()
        log.debug("normalizer_cache_fields_loaded", count=len(_cache["fields"]))

    return _cache["fields"]


def get_cached_values(field_name: str) -> list[dict]:
    """
    Gibt alle canonical values für ein Feld zurück (aus Cache oder DB).
    """
    if not NORMALIZE_CACHE_ENABLED:
        from normalizer_db import get_values
        return get_values(field_name)

    _check_and_invalidate()

    if field_name not in _cache["values"]:
        from normalizer_db import get_values
        _cache["values"][field_name] = get_values(field_name)
        log.debug(
            "normalizer_cache_values_loaded",
            field=field_name,
            count=len(_cache["values"][field_name]),
        )

    return _cache["values"][field_name]


def get_cached_mapping(template_name: str) -> Optional[dict]:
    """
    Gibt normalize_mapping + required_normalized_fields eines Templates zurück.
    Gibt None zurück wenn kein Mapping existiert.
    """
    if not NORMALIZE_CACHE_ENABLED:
        from normalizer_db import get_mapping
        return get_mapping(template_name)

    _check_and_invalidate()

    if template_name not in _cache["mappings"]:
        from normalizer_db import get_mapping
        _cache["mappings"][template_name] = get_mapping(template_name)
        log.debug(
            "normalizer_cache_mapping_loaded",
            template=template_name,
            found=_cache["mappings"][template_name] is not None,
        )

    return _cache["mappings"][template_name]


def get_cached_categories() -> list[dict]:
    """
    Gibt alle aktiven normalized_categories zurück (aus Cache oder DB).
    """
    if not NORMALIZE_CACHE_ENABLED:
        from normalizer_db import get_categories
        return get_categories()

    _check_and_invalidate()

    if _cache["categories"] is None:
        from normalizer_db import get_categories
        _cache["categories"] = get_categories()
        log.debug(
            "normalizer_cache_categories_loaded",
            count=len(_cache["categories"]),
        )

    return _cache["categories"]
