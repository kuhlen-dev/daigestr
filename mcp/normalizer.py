"""
Normalizer — 13-Schritte Normalisierung von extrahierten Dokumentdaten (T-DAI-053).

async def normalize(extracted, template_name, meta, compact) -> dict | None
"""

import re
import warnings
from datetime import datetime
from typing import Any, Optional

import structlog

from settings import (
    NORMALIZE_FALLBACK_COUNTRY,
    NORMALIZE_PLAUSIBILITY_TOLERANCE,
)
from normalizer_cache import (
    get_cached_fields,
    get_cached_values,
    get_cached_mapping,
    get_cached_categories,
)
from normalizer_db import find_canonical
from templates_db import get_scoring_weight

log = structlog.get_logger()

# =============================================================================
# Helper Functions
# =============================================================================

def _resolve_dot_path(obj: Any, path: str) -> Any:
    """
    Löst einen Dot-Notation-Pfad in einem verschachtelten Dict auf.
    z.B. "_meta.absender.firma" → obj["_meta"]["absender"]["firma"]
    Gibt None zurück wenn Pfad nicht gefunden.
    """
    if obj is None:
        return None
    parts = path.split(".")
    current = obj
    for part in parts:
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def _flatten_address(obj: Any) -> Optional[str]:
    """Flatten an address dict to a single string.
    {strasse: 'Musterstr. 42', plz: '12345', ort: 'Berlin'} → 'Musterstr. 42, 12345 Berlin'
    """
    if not isinstance(obj, dict):
        return str(obj) if obj is not None else None
    parts = []
    strasse = obj.get("strasse")
    if strasse:
        parts.append(str(strasse))
    plz = obj.get("plz")
    ort = obj.get("ort")
    if plz and ort:
        parts.append(f"{plz} {ort}")
    elif ort:
        parts.append(str(ort))
    elif plz:
        parts.append(str(plz))
    land = obj.get("land")
    if land:
        parts.append(str(land))
    return ", ".join(parts) if parts else None


def _convert_decimal(value: Any) -> Optional[float]:
    """
    Konvertiert einen Wert zu float (locale-aware).
    Unterstützt: "29,95" → 29.95, "1.234,56" → 1234.56, "29.95" → 29.95
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    # Entferne Währungssymbole und Leerzeichen
    s = re.sub(r"[€$£¥\s]", "", s)
    if not s:
        return None
    # Deutsches Format: "1.234,56" → Punkt als Tausender, Komma als Dezimal
    if re.match(r"^\d{1,3}(\.\d{3})+(,\d+)?$", s):
        s = s.replace(".", "").replace(",", ".")
    # Komma als Dezimaltrenner ohne Tausender: "29,95"
    elif re.match(r"^\d+,\d+$", s):
        s = s.replace(",", ".")
    # Nur Punkt: "29.95" — normal
    try:
        return float(s)
    except ValueError:
        return None


def _convert_date(value: Any) -> Optional[str]:
    """
    Konvertiert diverse Datumsformate zu ISO 8601 (YYYY-MM-DD).
    Unterstützt: "01.03.2025", "2025-03-01", "03/01/2025", "1. März 2025", etc.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    s = str(value).strip()
    if not s:
        return None

    # Bereits ISO-Format
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s

    # Deutsche Monatsnamen
    de_months = {
        "januar": "01", "februar": "02", "märz": "03", "maerz": "03",
        "april": "04", "mai": "05", "juni": "06", "juli": "07",
        "august": "08", "september": "09", "oktober": "10",
        "november": "11", "dezember": "12",
    }
    for name, num in de_months.items():
        if name in s.lower():
            s_clean = re.sub(r"[^\d\s]", " ", s.lower())
            parts = s_clean.split()
            nums = [p for p in parts if p.isdigit()]
            if len(nums) >= 2:
                day = nums[0].zfill(2)
                year = nums[-1] if len(nums[-1]) == 4 else nums[1]
                return f"{year}-{num}-{day}"

    # DD.MM.YYYY oder D.M.YYYY
    m = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$", s)
    if m:
        return f"{m.group(3)}-{m.group(2).zfill(2)}-{m.group(1).zfill(2)}"

    # MM/DD/YYYY
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", s)
    if m:
        return f"{m.group(3)}-{m.group(1).zfill(2)}-{m.group(2).zfill(2)}"

    # YYYY/MM/DD
    m = re.match(r"^(\d{4})/(\d{2})/(\d{2})$", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

    return None


def _convert_boolean(value: Any) -> Optional[bool]:
    """
    Konvertiert diverse Wahrheitswerte zu bool.
    "ja", "yes", "true", "1", "wahr" → True
    "nein", "no", "false", "0", "falsch" → False
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in {"ja", "yes", "true", "1", "wahr", "oui", "si"}:
        return True
    if s in {"nein", "no", "false", "0", "falsch", "non"}:
        return False
    return None


def _validate_iban(value: str) -> bool:
    """MOD-97 IBAN-Validierung."""
    iban = re.sub(r"\s", "", value.upper())
    if len(iban) < 15 or len(iban) > 34:
        return False
    rearranged = iban[4:] + iban[:4]
    converted = ""
    for ch in rearranged:
        if ch.isdigit():
            converted += ch
        elif ch.isalpha():
            converted += str(ord(ch) - ord("A") + 10)
        else:
            return False
    try:
        return int(converted) % 97 == 1
    except ValueError:
        return False


def _validate_iso4217(value: str) -> bool:
    """Prüft ob value ein gültiger ISO 4217 Währungscode ist (Länge 3 Alpha)."""
    return bool(re.match(r"^[A-Z]{3}$", value.upper()))


def _validate_iso8601(value: str) -> bool:
    """Prüft ob value ein gültiges ISO 8601 Datum ist (YYYY-MM-DD)."""
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}$", value))


def _validate_field(field_def: dict, value: Any) -> list[str]:
    """
    Validiert einen Wert gegen die validation_rules eines Feldes.
    Gibt Liste von Fehlermeldungen zurück (leer = OK).
    """
    errors = []
    rules = field_def.get("validation_rules") or {}
    if not rules or value is None:
        return errors

    # range: {"min": 0, "max": 100}
    if "min" in rules or "max" in rules:
        try:
            fval = float(value)
            if "min" in rules and fval < rules["min"]:
                errors.append(f"value {fval} below min {rules['min']}")
            if "max" in rules and fval > rules["max"]:
                errors.append(f"value {fval} above max {rules['max']}")
        except (TypeError, ValueError):
            pass

    # pattern: regex string
    if "pattern" in rules:
        if not re.match(rules["pattern"], str(value)):
            errors.append(f"value does not match pattern {rules['pattern']}")

    # mod97 (IBAN)
    if rules.get("mod97"):
        if not _validate_iban(str(value)):
            errors.append("IBAN failed MOD-97 check")

    # iso4217 (currency)
    if rules.get("iso4217"):
        if not _validate_iso4217(str(value)):
            errors.append(f"'{value}' is not a valid ISO 4217 currency code")

    # iso8601 (date)
    if rules.get("iso8601"):
        if not _validate_iso8601(str(value)):
            errors.append(f"'{value}' is not a valid ISO 8601 date")

    return errors


# =============================================================================
# Main Normalizer
# =============================================================================

async def normalize(
    extracted: dict,
    template_name: str,
    meta: dict,
    compact: bool = False,
) -> Optional[dict]:
    """
    Normalisiert ein extrahiertes Dict in 13 Schritten.

    Args:
        extracted:      Rohdaten aus der Extraktion
        template_name:  Template-ID (z.B. "invoice")
        meta:           Dokument-Metadaten (language, filename, etc.)
        compact:        Wenn True: null-Felder aus Output entfernen

    Returns:
        dict mit normalized, normalized_version, normalized_warnings,
        normalized_trace, normalized_context — oder None bei Fehler.
    """
    warnings_list: list[str] = []
    trace: list[dict] = []

    # -------------------------------------------------------------------------
    # Schritt 0: Vorprüfung
    # -------------------------------------------------------------------------
    if extracted is None:
        log.warning("normalizer_skip_no_extracted", template=template_name)
        return None

    mapping_data = get_cached_mapping(template_name)
    if not mapping_data or not mapping_data.get("normalize_mapping"):
        log.warning("normalizer_skip_no_mapping", template=template_name)
        warnings_list.append(f"No normalize_mapping found for template '{template_name}'")
        return {
            "normalized": None,
            "normalized_version": None,
            "normalized_warnings": warnings_list,
            "normalized_trace": None,
            "normalized_context": None,
            "normalized_confidence": None,
            "quality_score": None,
            "validation_errors": [],
        }

    # -------------------------------------------------------------------------
    # Schritt 1: Template + Mapping laden
    # -------------------------------------------------------------------------
    mapping: dict = mapping_data["normalize_mapping"]
    required_fields: list[str] = mapping_data.get("required_normalized_fields") or []
    all_fields = get_cached_fields()
    fields_by_name: dict[str, dict] = {f["name"]: f for f in all_fields}

    # -------------------------------------------------------------------------
    # Schritt 2: Kontext ableiten
    # -------------------------------------------------------------------------
    context: dict = {}
    # Vendor country aus Extracted oder Meta
    vendor_country = (
        _resolve_dot_path(extracted, "vendor_country")
        or _resolve_dot_path(extracted, "_meta.absender.land")
        or meta.get("vendor_country")
    )
    recipient_country = (
        _resolve_dot_path(extracted, "_meta.empfaenger.land")
        or _resolve_dot_path(extracted, "recipient_country")
        or meta.get("recipient_country")
    )
    context["vendor_country"] = vendor_country or NORMALIZE_FALLBACK_COUNTRY
    context["recipient_country"] = recipient_country or NORMALIZE_FALLBACK_COUNTRY
    context["document_quality_score"] = meta.get("quality_score")
    # tax_country wird in Schritt 7 gesetzt

    # -------------------------------------------------------------------------
    # Schritt 3: Feld-Mapping (Dot-Notation Pfade auflösen)
    # -------------------------------------------------------------------------
    raw_values: dict[str, Any] = {}
    for norm_field, source_path in mapping.items():
        if isinstance(source_path, str):
            val = _resolve_dot_path(extracted, source_path)
            raw_values[norm_field] = val
        elif isinstance(source_path, list):
            # Fallback-Kette: erstes nicht-None gewinnt
            val = None
            for path in source_path:
                val = _resolve_dot_path(extracted, path)
                if val is not None:
                    break
            raw_values[norm_field] = val

    # -------------------------------------------------------------------------
    # Schritt 3a: meta_path Fallback — null-Felder über _meta auffüllen
    # -------------------------------------------------------------------------
    for field_name in list(raw_values.keys()):
        if raw_values[field_name] is None:
            field_def = fields_by_name.get(field_name)
            if field_def and field_def.get("meta_path"):
                val = _resolve_dot_path(extracted, field_def["meta_path"])
                if val is not None:
                    raw_values[field_name] = val
                    trace.append({
                        "field": field_name,
                        "source_field": field_def["meta_path"],
                        "raw": val,
                        "rule": "meta_path_fallback",
                        "confidence": 0.8,
                    })

    # Auch Felder mit meta_path die NICHT im Mapping stehen
    for field_name, field_def in fields_by_name.items():
        if field_name not in raw_values and field_def.get("meta_path"):
            val = _resolve_dot_path(extracted, field_def["meta_path"])
            if val is not None:
                raw_values[field_name] = val
                trace.append({
                    "field": field_name,
                    "source_field": field_def["meta_path"],
                    "raw": val,
                    "rule": "meta_path_universal",
                    "confidence": 0.7,
                })

    # -------------------------------------------------------------------------
    # Schritt 3b: Flatten dict values for string fields (e.g. address objects)
    # -------------------------------------------------------------------------
    for field_name, val in list(raw_values.items()):
        if isinstance(val, dict):
            field_def = fields_by_name.get(field_name)
            if field_def and field_def.get("type") == "string":
                raw_values[field_name] = _flatten_address(val)
                trace.append({
                    "field": field_name,
                    "source_field": mapping.get(field_name),
                    "raw": val,
                    "rule": "flatten_dict_to_string",
                    "confidence": 0.9,
                })

    # -------------------------------------------------------------------------
    # Schritt 4: Wert-Normalisierung via find_canonical für Enum-Felder
    # -------------------------------------------------------------------------
    normalized: dict[str, Any] = {}
    for field_name, raw_val in raw_values.items():
        field_def = fields_by_name.get(field_name)
        if field_def and field_def.get("type") == "enum" and raw_val is not None:
            canonical = find_canonical(field_name, str(raw_val))
            if canonical is not None:
                normalized[field_name] = canonical
                trace.append({
                    "field": field_name,
                    "source_field": field_name,
                    "raw": raw_val,
                    "rule": "find_canonical",
                    "confidence": 1.0,
                })
            else:
                normalized[field_name] = raw_val
                warnings_list.append(
                    f"No canonical value found for '{field_name}' = '{raw_val}'"
                )
                trace.append({
                    "field": field_name,
                    "source_field": field_name,
                    "raw": raw_val,
                    "rule": "passthrough_no_canonical",
                    "confidence": 0.5,
                })
        else:
            normalized[field_name] = raw_val

    # -------------------------------------------------------------------------
    # Schritt 5: Typ-Konvertierung
    # -------------------------------------------------------------------------
    for field_name, val in list(normalized.items()):
        if val is None:
            continue
        field_def = fields_by_name.get(field_name)
        if not field_def:
            continue
        ftype = field_def.get("type", "string")
        converted = val
        rule = None

        if ftype == "decimal":
            converted = _convert_decimal(val)
            rule = "convert_decimal"
        elif ftype == "date":
            converted = _convert_date(val)
            rule = "convert_date"
        elif ftype == "boolean":
            converted = _convert_boolean(val)
            rule = "convert_boolean"

        if converted != val:
            normalized[field_name] = converted
            trace.append({
                "field": field_name,
                "source_field": field_name,
                "raw": val,
                "rule": rule,
                "confidence": 0.9,
            })

    # -------------------------------------------------------------------------
    # Schritt 6: line_items Sub-Normalisierung
    # -------------------------------------------------------------------------
    item_mapping = mapping.get("_item_mapping")
    if item_mapping and "line_items" in raw_values:
        raw_items = raw_values.get("line_items") or []
        if isinstance(raw_items, list):
            norm_items = []
            for item in raw_items:
                if not isinstance(item, dict):
                    norm_items.append(item)
                    continue
                norm_item: dict = {}
                for norm_key, src_key in item_mapping.items():
                    item_val = item.get(src_key) if isinstance(src_key, str) else None
                    if item_val is not None:
                        # Typ-Konvertierung für Item-Felder
                        item_field = fields_by_name.get(norm_key)
                        if item_field:
                            ftype = item_field.get("type", "string")
                            if ftype == "decimal":
                                item_val = _convert_decimal(item_val)
                            elif ftype == "date":
                                item_val = _convert_date(item_val)
                            elif ftype == "boolean":
                                item_val = _convert_boolean(item_val)
                    norm_item[norm_key] = item_val
                norm_items.append(norm_item)
            normalized["line_items"] = norm_items

    # -------------------------------------------------------------------------
    # Schritt 7: Computed Fields
    # -------------------------------------------------------------------------
    line_items = normalized.get("line_items")
    if isinstance(line_items, list):
        normalized["line_items_count"] = len(line_items)

    # page_count aus Dokument-Metadaten
    if normalized.get("page_count") is None:
        pc = meta.get("pages_processed") or meta.get("page_count")
        if pc is not None:
            try:
                normalized["page_count"] = int(pc)
            except (ValueError, TypeError):
                pass

    # language aus Request-Parameter
    if normalized.get("language") is None:
        lang = meta.get("language")
        if lang:
            normalized["language"] = lang

    # tax_country: vendor_country wenn Transaktion innerhalb DE, sonst vendor_country
    tax_country = context["vendor_country"]
    context["tax_country"] = tax_country
    if "tax_country" not in normalized:
        normalized["tax_country"] = tax_country

    # -------------------------------------------------------------------------
    # Schritt 8: Defaults setzen
    # -------------------------------------------------------------------------
    # Fallback-Country
    for country_field in ("vendor_country", "recipient_country"):
        if normalized.get(country_field) is None and country_field in fields_by_name:
            normalized[country_field] = NORMALIZE_FALLBACK_COUNTRY
            trace.append({
                "field": country_field,
                "source_field": None,
                "raw": None,
                "rule": "default_fallback_country",
                "confidence": 0.3,
            })

    # Währungs-Default aus DB (Country → Currency Mapping)
    if normalized.get("currency") is None and "currency" in fields_by_name:
        vendor_c = normalized.get("vendor_country", NORMALIZE_FALLBACK_COUNTRY)
        currency = find_canonical("_country_currency", vendor_c)
        if currency:
            normalized["currency"] = currency
            trace.append({
                "field": "currency",
                "source_field": None,
                "raw": None,
                "rule": "default_currency_from_country",
                "confidence": 0.7,
            })

    # DB-Defaults für alle anderen Felder
    for field_name, field_def in fields_by_name.items():
        if field_name in normalized:
            continue
        if field_def.get("default_value") is not None:
            # Nur setzen wenn im Mapping definiert
            if field_name in mapping:
                normalized[field_name] = field_def["default_value"]

    # -------------------------------------------------------------------------
    # Schritt 9: Validierung (validation_rules aus normalized_fields)
    # -------------------------------------------------------------------------
    validation_errors: list[dict] = []
    for field_name, val in normalized.items():
        if val is None:
            continue
        field_def = fields_by_name.get(field_name)
        if not field_def:
            continue
        errors = _validate_field(field_def, val)
        for err in errors:
            validation_errors.append({"field": field_name, "value": val, "error": err})
            warnings_list.append(f"Validation error on '{field_name}': {err}")

    # Required fields prüfen
    for req_field in required_fields:
        if normalized.get(req_field) is None:
            warnings_list.append(f"Required field '{req_field}' is missing or null")

    # -------------------------------------------------------------------------
    # Schritt 10: Cross-Field Plausibilitätsprüfung
    # -------------------------------------------------------------------------
    amount = normalized.get("amount")
    amount_net = normalized.get("amount_net")
    amount_tax = normalized.get("amount_tax")
    if (
        amount is not None
        and amount_net is not None
        and amount_tax is not None
    ):
        expected = amount_net + amount_tax
        diff = abs(amount - expected)
        tolerance = NORMALIZE_PLAUSIBILITY_TOLERANCE * max(abs(amount), 0.01)
        if diff > tolerance:
            warnings_list.append(
                f"Plausibility check failed: amount ({amount}) ≠ "
                f"amount_net ({amount_net}) + amount_tax ({amount_tax}) "
                f"[diff={diff:.4f}, tolerance={tolerance:.4f}]"
            )

    # -------------------------------------------------------------------------
    # Schritt 11: Quality Score
    # -------------------------------------------------------------------------
    total_weight = 0.0
    scored_weight = 0.0
    for field_name in mapping:
        if field_name.startswith("_"):
            continue
        try:
            weight = get_scoring_weight(field_name) or 1.0
        except Exception:
            weight = 1.0
        total_weight += weight
        val = normalized.get(field_name)
        if val is not None:
            scored_weight += weight

    quality_score = round(scored_weight / total_weight, 3) if total_weight > 0 else 0.0

    # completeness_score: Anteil belegter Felder an allen gemappten Feldern
    total_mapped = len([f for f in mapping if not f.startswith("_")])
    filled_mapped = len([f for f in mapping if not f.startswith("_") and normalized.get(f) is not None])
    completeness_score = round(filled_mapped / total_mapped, 3) if total_mapped > 0 else 0.0

    # -------------------------------------------------------------------------
    # Schritt 12: Traceability vervollständigen
    # -------------------------------------------------------------------------
    # Felder ohne Trace-Eintrag bekommen einen generischen Eintrag
    traced_fields = {t["field"] for t in trace}
    for field_name, val in normalized.items():
        if field_name in traced_fields or field_name.startswith("_"):
            continue
        trace.append({
            "field": field_name,
            "source_field": mapping.get(field_name),
            "raw": raw_values.get(field_name),
            "rule": "direct_mapping",
            "confidence": 1.0 if val is not None else 0.0,
        })

    # -------------------------------------------------------------------------
    # Schritt 13: Output zusammenbauen
    # -------------------------------------------------------------------------
    # Normalization-Version: Hash aus Template + DB-State
    try:
        from normalizer_cache import _meta as cache_meta
        normalized_version = cache_meta.get("version_hash") or "unknown"
    except Exception:
        normalized_version = "unknown"

    # Quality score + validation errors in context (accessible via normalized_context)
    context["quality_score"] = quality_score
    context["validation_errors"] = validation_errors

    normalized["_quality_score"] = quality_score
    normalized["quality_score"] = quality_score
    normalized["completeness_score"] = completeness_score

    confidence_dict: dict[str, float] = {}
    for t in trace:
        field = t.get("field")
        conf = t.get("confidence")
        if field and conf is not None:
            confidence_dict[field] = conf

    if compact:
        # Null-Felder entfernen
        normalized = {k: v for k, v in normalized.items() if v is not None}

    return {
        "normalized": normalized,
        "normalized_version": normalized_version,
        "normalized_warnings": warnings_list,
        "normalized_trace": trace,
        "normalized_context": context,
        "normalized_confidence": confidence_dict,
        "quality_score": quality_score,
        "validation_errors": validation_errors,
    }
