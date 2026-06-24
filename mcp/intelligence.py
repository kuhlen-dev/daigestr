"""
Daigestr — Document Intelligence

Enthält LLM-gestützte Analyse-Funktionen:
- classify_document: Dokumenttyp-Klassifizierung
- correct_ocr_text: OCR-Nachkorrektur
- extract_structured_data: Schema-basierte Datenextraktion
- calculate_quality_score: Qualitäts-Score-Berechnung
- chunk_markdown: Smart Chunking für RAG
- dual_pass_validate: Dual-Pass Vision-Validierung
- get_classify_categories_from_db: Template-Registry Kategorien
- find_matching_template: Template-Matching
- _make_null_tolerant: JSON-Schema Hilfsfunktion
- _apply_auto_extract: Auto-Extraktion nach Konvertierung
"""

import copy
import json
import re
import sys
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

import structlog

try:
    import jsonschema
    JSONSCHEMA_AVAILABLE = True
except ImportError:
    JSONSCHEMA_AVAILABLE = False

from settings import (
    MISTRAL_API_KEY,
    MISTRAL_TEXT_MODEL,
    CLASSIFY_MAX_TOKENS,
    EXTRACT_MAX_TOKENS,
    OCR_CORRECT_MAX_TOKENS,
    CLASSIFY_MAX_CHARS,
    EXTRACT_MAX_CHARS,
    MISTRAL_EXTRACT_RESPONSE_FORMAT,
    _classify_categories_cache,
    _CLASSIFY_CACHE_TTL,
)
from mistral_client import call_mistral_vision_api, analyze_with_mistral_vision
from utils import strip_llm_artifacts, _get, _LOADED_BY_SERVER  # noqa: F401
from models import MetaData, ConvertResponse, ErrorCode, create_error_response

log = structlog.get_logger()


# =============================================================================
# _meta Block: Steuerrelevanz + Datentyp-Konventionen (T-DAI-062)
# Alle Werte werden aus der DB geladen (prompt-Tabelle).
# Daigestr ist DB-first/DB-only: fehlende Konfiguration ist ein echter Fehler.
# =============================================================================

def _require_db_prompt(category: str, name: str, language: str = "de") -> str:
    """Load a DB-backed prompt via the server override hook."""
    from templates_db import get_prompt as _get_prompt  # noqa: PLC0415 — lazy import

    return _get("get_prompt", _get_prompt)(category, name, language=language)


def _render_prompt_template(template: str, **values: Any) -> str:
    """Render known DB placeholders without breaking on literal JSON braces."""
    sentinel_map: dict[str, Any] = {}
    rendered = template
    for key, value in values.items():
        placeholder = "{" + key + "}"
        sentinel = f"__DAIGESTR_PROMPT_{key.upper()}__"
        rendered = rendered.replace(placeholder, sentinel)
        sentinel_map[sentinel] = value

    rendered = rendered.replace("{", "{{").replace("}", "}}")

    for sentinel in sentinel_map:
        rendered = rendered.replace(sentinel, "{" + sentinel + "}")

    return rendered.format(**sentinel_map)


def _load_meta_schema() -> dict:
    """Load _META_SCHEMA from DB (prompt id='meta.schema'). Cached after first call."""
    raw = _require_db_prompt("meta", "schema", language="de")
    return json.loads(raw)

_META_SCHEMA: dict | None = None

def get_meta_schema() -> dict:
    """Returns cached _META_SCHEMA, loading from DB on first call."""
    global _META_SCHEMA
    if _META_SCHEMA is None:
        _META_SCHEMA = _load_meta_schema()
    return _META_SCHEMA


def _meta_description_to_json_schema(node: Any) -> dict[str, Any]:
    """Convert the descriptive DB-backed _meta schema into a valid JSON Schema."""
    if isinstance(node, dict):
        return {
            "type": "object",
            "properties": {key: _meta_description_to_json_schema(value) for key, value in node.items()},
            "additionalProperties": False,
        }
    if isinstance(node, list):
        if node:
            return {"type": "array", "items": _meta_description_to_json_schema(node[0])}
        return {"type": "array", "items": {"type": ["string", "null"]}}
    return {"type": ["string", "null"], "description": str(node)}


def _as_non_empty_string(value: Any) -> Optional[str]:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def _looks_like_bank_statement(extracted: dict[str, Any]) -> bool:
    if not isinstance(extracted, dict):
        return False
    return (
        isinstance(extracted.get("kontoauszuege"), list)
        or (
            "auszugsnummer" in extracted
            and "buchungen" in extracted
            and ("iban" in extracted or "bank" in extracted)
        )
    )


def _has_meaningful_extracted_payload(extracted: Any) -> bool:
    """Return True when extracted data contains meaningful non-meta content."""
    if not isinstance(extracted, dict) or not extracted:
        return False

    def _value_present(value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, dict):
            return any(
                _value_present(child)
                for key, child in value.items()
                if not str(key).startswith("_")
            )
        if isinstance(value, list):
            return any(_value_present(item) for item in value)
        return True

    return any(
        _value_present(value)
        for key, value in extracted.items()
        if not str(key).startswith("_")
    )


def _format_german_amount(amount: Any) -> Optional[str]:
    raw = _as_non_empty_string(amount)
    if raw is None:
        return None
    sign = ""
    if raw.startswith(("+", "-")):
        sign = raw[0]
        raw = raw[1:]
    if "." in raw:
        integer_part, decimal_part = raw.split(".", 1)
    else:
        integer_part, decimal_part = raw, "00"
    integer_part = integer_part or "0"
    try:
        grouped = f"{int(integer_part):,}".replace(",", ".")
    except ValueError:
        return sign + raw
    decimal_part = decimal_part[:2].ljust(2, "0")
    return f"{sign}{grouped},{decimal_part}"


def _merge_bank_statement_bookings(bookings: Any, default_currency: Optional[str]) -> list[dict[str, Any]]:
    if not isinstance(bookings, list):
        return []

    enriched: list[tuple[str, int, dict[str, Any]]] = []
    for index, booking in enumerate(bookings):
        if not isinstance(booking, dict):
            continue
        normalized_booking = copy.deepcopy(booking)
        if normalized_booking.get("währung") is None and default_currency:
            normalized_booking["währung"] = default_currency
        booking_date = _as_non_empty_string(normalized_booking.get("datum")) or "9999-99-99"
        enriched.append((booking_date, index, normalized_booking))

    enriched.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in enriched]


def _extract_bank_statement_page_groups(markdown: str) -> list[tuple[str, str]]:
    """Split markdown into per-statement page groups for bundled bank-statement PDFs."""
    if not isinstance(markdown, str) or not markdown.strip():
        return []

    pages = [
        chunk.strip()
        for chunk in re.split(r"(?=^## Seite \d+\s*$)", markdown, flags=re.MULTILINE)
        if chunk.strip()
    ]
    if not pages:
        return []

    groups: list[tuple[str, list[str]]] = []
    current_number: Optional[str] = None

    for page in pages:
        matches = re.findall(r"Kontoauszug\s+(\d+)", page, flags=re.IGNORECASE)
        statement_number = matches[0] if matches else current_number
        if statement_number is None:
            continue
        current_number = statement_number
        if not groups or groups[-1][0] != statement_number:
            groups.append((statement_number, [page]))
        else:
            groups[-1][1].append(page)

    return [
        (statement_number, "\n\n".join(group_pages))
        for statement_number, group_pages in groups
        if statement_number and group_pages
    ]


def _normalize_bank_statement_amount(
    raw: Any,
    *,
    inferred_sign: Optional[str] = None,
) -> Optional[str]:
    value = _as_non_empty_string(raw)
    if value is None:
        return None
    cleaned = (
        value.replace("EUR", "")
        .replace("€", "")
        .replace(" ", "")
        .replace("|", "")
        .strip()
    )
    simple_decimal = re.fullmatch(r"([+-])?(\d+\.\d{2})", cleaned)
    if simple_decimal:
        sign = simple_decimal.group(1) or inferred_sign or ""
        numeric = simple_decimal.group(2)
        if sign == "-":
            return f"-{numeric}"
        if sign == "+":
            return f"+{numeric}"
        return numeric

    match = re.search(r"([+-])?(\d{1,3}(?:\.\d{3})*,\d{2}|\d+,\d{2})([+-])?", cleaned)
    if not match:
        return None
    sign = match.group(1) or match.group(3) or inferred_sign or ""
    numeric = match.group(2)
    if "," in numeric:
        numeric = numeric.replace(".", "").replace(",", ".")
    if sign == "-":
        return f"-{numeric}"
    if sign == "+":
        return f"+{numeric}"
    return numeric


def _bank_statement_amount_to_decimal(amount: Any) -> Optional[Decimal]:
    normalized = _normalize_bank_statement_amount(amount)
    if normalized is None:
        return None
    try:
        return Decimal(normalized)
    except InvalidOperation:
        return None


def _format_bank_statement_decimal(amount: Decimal) -> str:
    quantized = amount.quantize(Decimal("0.01"))
    sign = "+" if quantized >= 0 else "-"
    absolute = abs(quantized)
    return f"{sign}{absolute:.2f}"


def _bank_statement_date_to_iso(value: str, fallback_year: Optional[str] = None) -> Optional[str]:
    raw = _as_non_empty_string(value)
    if raw is None:
        return None
    full_match = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", raw)
    if full_match:
        day, month, year = full_match.groups()
        return f"{year}-{month}-{day}"
    short_match = re.search(r"(\d{2})\.(\d{2})", raw)
    if short_match and fallback_year:
        day, month = short_match.groups()
        return f"{fallback_year}-{month}-{day}"
    return None


def _extract_bank_statement_common_fields(markdown: str) -> dict[str, Any]:
    extracted: dict[str, Any] = {}

    bank_match = re.search(r"\n([^\n]+?)\s+UST-ID\b", markdown, flags=re.IGNORECASE)
    bank_name = _as_non_empty_string(bank_match.group(1)) if bank_match else None
    if bank_name:
        extracted["bank"] = bank_name

    iban_match = re.search(
        r"IBAN:\s*\n?\s*([A-Z0-9 ]{12,48})",
        markdown,
        flags=re.IGNORECASE,
    )
    iban = None
    if iban_match:
        iban_line = iban_match.group(1).splitlines()[0]
        iban = re.sub(r"\s+", "", iban_line).strip()
    if iban:
        extracted["iban"] = iban

    bic_match = re.search(r"(?:SWIFT-)?BIC:\s*([A-Z0-9]{8,11})", markdown, flags=re.IGNORECASE)
    bic = _as_non_empty_string(bic_match.group(1)) if bic_match else None
    if bic:
        extracted["bic"] = bic

    holder_match = re.search(
        r"\n([A-ZÄÖÜa-zäöüß][^\n]*?)\n\s*IBAN:",
        markdown,
        flags=re.IGNORECASE,
    )
    holder_name = _as_non_empty_string(holder_match.group(1)) if holder_match else None
    if holder_name:
        extracted["kontoinhaber"] = {"name": holder_name}

    if bic or iban:
        extracted["währung"] = "EUR"

    return extracted


def _infer_bank_statement_booking_sign(text: str) -> str:
    lowered = text.lower()
    positive_markers = (
        "gutschrift",
        "ps-sparen",
        "habenzins",
        "zinsgutschrift",
        "eingang",
    )
    if any(marker in lowered for marker in positive_markers):
        return "+"
    return "-"


def _looks_like_bank_statement_noise_line(line: str) -> bool:
    lowered = line.lower()
    if not lowered:
        return True
    if lowered.startswith(("## seite", "komfortkonto", "stadtsparkasse", "datum erläuterungen", "blatt", "betrag", "iban:", "swift-bic:", "www.")):
        return True
    if lowered.startswith("kontostand "):
        return True
    if lowered == "max und erika mustermann":
        return True
    if re.fullmatch(r"\d+(?:\s+\d+)*", line):
        return True
    if re.fullmatch(r"DE\d{2}(?:\s*\d{4}){4,8}", line):
        return True
    if re.fullmatch(r"[A-Z0-9]{8,11}", line):
        return True
    if re.fullmatch(r"\|?\s*-+\s*(?:\|\s*-+\s*)+\|?", line):
        return True
    return False


def _extract_bank_statement_segment_opening_balance(statement_markdown: str) -> Optional[str]:
    match = re.search(
        r"Kontoauszug\s+\d+.*?Betrag\s*([\d\.,]+[+-])",
        statement_markdown,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None
    return _normalize_bank_statement_amount(match.group(1))


def _extract_bank_statement_segment_statement_date(statement_markdown: str) -> Optional[str]:
    matches = re.findall(r"Kontostand(?: am)?\s*(\d{2}\.\d{2}\.\d{4})", statement_markdown, flags=re.IGNORECASE)
    if matches:
        return _bank_statement_date_to_iso(matches[-1])
    booking_dates = re.findall(r"Wert:\s*(\d{2}\.\d{2}\.\d{4})", statement_markdown, flags=re.IGNORECASE)
    if booking_dates:
        return _bank_statement_date_to_iso(booking_dates[-1])
    return None


def _extract_bank_statement_segment_statement_number(statement_markdown: str) -> Optional[str]:
    match = re.search(r"Kontoauszug\s+(\d+)", statement_markdown, flags=re.IGNORECASE)
    if not match:
        return None
    return _as_non_empty_string(match.group(1))


def _parse_bank_statement_markdown_bookings(statement_markdown: str) -> list[dict[str, Any]]:
    lines = [line.strip().strip("|").strip() for line in statement_markdown.splitlines()]
    bookings: list[dict[str, Any]] = []
    current: Optional[dict[str, Any]] = None

    def _finalize_current() -> None:
        nonlocal current
        if current is None:
            return
        text_parts = current.pop("_text_parts", [])
        canonical_text = " ".join(part for part in text_parts if part).strip()
        current["text"] = canonical_text or current.get("booking_type") or "Kontobewegung"
        bookings.append(current)
        current = None

    def _match_start(line: str) -> Optional[re.Match[str]]:
        return re.match(
            r"^(?:\|?\s*)?(\d{2}\.\d{2})\s+(.+?)(?:\s+Wert:\s*(\d{2}\.\d{2}\.\d{4}))?(?:\s+([\d\.,]+[+-]?))?$",
            line,
            flags=re.IGNORECASE,
        )

    def _assign_amount(amount_value: str) -> None:
        normalized = _normalize_bank_statement_amount(amount_value)
        if normalized is None:
            return
        for booking in bookings:
            if booking.get("amount") is None:
                booking["amount"] = normalized
                return
        if current is not None and current.get("amount") is None:
            current["amount"] = normalized

    for raw_line in lines:
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            continue

        start_match = _match_start(line)
        if start_match:
            _finalize_current()
            short_date, booking_type, value_date, inline_amount = start_match.groups()
            current = {
                "booking_date": short_date,
                "value_date": value_date,
                "booking_type": _as_non_empty_string(booking_type) or "Kontobewegung",
                "amount": _normalize_bank_statement_amount(inline_amount) if inline_amount else None,
                "_text_parts": [],
            }
            if current["booking_type"]:
                current["_text_parts"].append(current["booking_type"])
            continue

        if current is not None:
            value_match = re.search(r"Wert:\s*(\d{2}\.\d{2}\.\d{4})(?:\s+([\d\.,]+[+-]?))?", line, flags=re.IGNORECASE)
            if value_match:
                current["value_date"] = value_match.group(1)
                if value_match.group(2):
                    current["amount"] = _normalize_bank_statement_amount(value_match.group(2))
                continue

        amount_only_match = re.fullmatch(r"([\d\.,]+[+-]?)", line)
        if amount_only_match:
            _assign_amount(amount_only_match.group(1))
            continue

        if _looks_like_bank_statement_noise_line(line):
            continue

        if current is not None:
            current["_text_parts"].append(line)

    _finalize_current()

    typed_bookings: list[dict[str, Any]] = []
    for booking in bookings:
        booking_date = booking.get("value_date") or booking.get("booking_date")
        iso_date = _bank_statement_date_to_iso(booking_date)
        text = _as_non_empty_string(booking.get("text"))
        amount = _normalize_bank_statement_amount(
            booking.get("amount"),
            inferred_sign=_infer_bank_statement_booking_sign(text or ""),
        )
        if iso_date is None or text is None or amount is None:
            continue
        typed_bookings.append({
            "datum": iso_date,
            "text": text,
            "betrag": amount,
            "währung": "EUR",
        })

    typed_bookings.sort(key=lambda item: item["datum"])
    return typed_bookings


def _reconstruct_bank_statement_segment_from_markdown(
    statement_markdown: str,
    *,
    next_statement_markdown: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    statement_number = _extract_bank_statement_segment_statement_number(statement_markdown)
    opening_balance = _extract_bank_statement_segment_opening_balance(statement_markdown)
    if statement_number is None or opening_balance is None:
        return None

    common_fields = _extract_bank_statement_common_fields(statement_markdown)
    bookings = _parse_bank_statement_markdown_bookings(statement_markdown)
    if not bookings:
        return None

    opening_decimal = _bank_statement_amount_to_decimal(opening_balance)
    if opening_decimal is None:
        return None

    running_balance = opening_decimal
    for booking in bookings:
        amount_decimal = _bank_statement_amount_to_decimal(booking.get("betrag"))
        if amount_decimal is None:
            return None
        running_balance += amount_decimal
        booking["saldo"] = _format_bank_statement_decimal(running_balance)

    next_opening_balance = None
    if next_statement_markdown:
        next_opening_balance = _extract_bank_statement_segment_opening_balance(next_statement_markdown)

    ending_balance = next_opening_balance or bookings[-1].get("saldo")
    if ending_balance is None:
        return None

    statement_date = _extract_bank_statement_segment_statement_date(statement_markdown) or bookings[-1]["datum"]
    period = {
        "von": bookings[0]["datum"],
        "bis": bookings[-1]["datum"],
    }

    reconstructed = {
        **common_fields,
        "auszugsnummer": statement_number,
        "datum": statement_date,
        "zeitraum": period,
        "anfangssaldo": opening_balance,
        "endsaldo": ending_balance,
        "währung": common_fields.get("währung") or "EUR",
        "buchungen": bookings,
        "_meta": {
            "dokumenten_id": statement_number,
            "zusammenfassung": f"Kontoauszug {statement_number} mit {len(bookings)} Buchungen.",
        },
    }
    return reconstructed


def _needs_bank_statement_bundle_recovery(markdown: str, extracted: dict[str, Any]) -> bool:
    page_groups = _extract_bank_statement_page_groups(markdown)
    detected_numbers = [number for number, _ in page_groups]
    unique_numbers = list(dict.fromkeys(detected_numbers))
    if len(unique_numbers) <= 1:
        return False

    if not _has_meaningful_extracted_payload(extracted):
        return True
    if not _looks_like_bank_statement(extracted):
        return False

    raw_statements = extracted.get("kontoauszuege")
    if isinstance(raw_statements, list) and len(raw_statements) >= len(unique_numbers):
        return False

    top_level_number = _as_non_empty_string(extracted.get("auszugsnummer"))
    if top_level_number and "," in top_level_number:
        materialized_numbers = [part.strip() for part in top_level_number.split(",") if part.strip()]
        if len(materialized_numbers) >= len(unique_numbers):
            return False

    return True


def _schema_supports_bank_statement_bundle(schema: dict[str, Any]) -> bool:
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(properties, dict):
        return False
    return all(key in properties for key in ("auszugsnummer", "buchungen", "kontoauszuege"))


async def _recover_bank_statement_bundle(
    markdown: str,
    schema: dict[str, Any],
    *,
    language: str,
    field_descriptions: Optional[dict | str],
    notes: Optional[str],
    request_id: Optional[str],
    attempt_number: Optional[int],
) -> Optional[dict[str, Any]]:
    if not _schema_supports_bank_statement_bundle(schema):
        return None

    page_groups = _extract_bank_statement_page_groups(markdown)
    unique_numbers = [number for number, _ in page_groups]
    if len(unique_numbers) <= 1:
        return None

    log.info(
        "extract_structured_data_bank_bundle_recovery_start",
        request_id=request_id,
        attempt_number=attempt_number,
        detected_statement_numbers=unique_numbers,
        detected_statement_count=len(unique_numbers),
    )

    recovered_statements: list[dict[str, Any]] = []
    recovery_tokens = 0
    reconstructed_statement_numbers: list[str] = []
    for index, (statement_number, statement_markdown) in enumerate(page_groups):
        next_statement_markdown = page_groups[index + 1][1] if index + 1 < len(page_groups) else None
        statement_result = await extract_structured_data(
            statement_markdown,
            schema,
            language=language,
            field_descriptions=field_descriptions,
            notes=notes,
            request_id=request_id,
            attempt_number=attempt_number,
            enable_bank_bundle_recovery=False,
            strict_schema_validation=False,
        )
        recovery_tokens += int(statement_result.get("tokens") or 0)
        statement_extracted = statement_result.get("extracted")
        if statement_result.get("success") and isinstance(statement_extracted, dict):
            recovered_statements.append(statement_extracted)
            continue

        reconstructed_statement = _reconstruct_bank_statement_segment_from_markdown(
            statement_markdown,
            next_statement_markdown=next_statement_markdown,
        )
        if reconstructed_statement is not None:
            reconstructed_statement_numbers.append(statement_number)
            recovered_statements.append(reconstructed_statement)
            log.warning(
                "extract_structured_data_bank_bundle_recovery_segment_reconstructed",
                request_id=request_id,
                attempt_number=attempt_number,
                statement_number=statement_number,
            )
            continue

        if not statement_result.get("success"):
            log.warning(
                "extract_structured_data_bank_bundle_recovery_failed",
                request_id=request_id,
                attempt_number=attempt_number,
                statement_number=statement_number,
                error=statement_result.get("error"),
            )
            return None
        if not isinstance(statement_extracted, dict):
            return None

    if not recovered_statements:
        return None

    extracted: dict[str, Any] = {
        "kontoauszuege": recovered_statements,
    }
    extracted = _harmonize_extracted_summary_fields(extracted)
    if reconstructed_statement_numbers:
        meta_block = extracted.get("_meta")
        if not isinstance(meta_block, dict):
            meta_block = {}
            extracted["_meta"] = meta_block
        reconstructed_label = ",".join(reconstructed_statement_numbers)
        meta_block["reconstructed_statement_numbers"] = reconstructed_label
        meta_block["reconstructed_from_markdown"] = True
    log.info(
        "extract_structured_data_bank_bundle_recovery_success",
        request_id=request_id,
        attempt_number=attempt_number,
        recovered_statement_count=len(recovered_statements),
        reconstructed_statement_count=len(reconstructed_statement_numbers),
        tokens=recovery_tokens,
    )
    return {
        "success": True,
        "extracted": extracted,
        "tokens": recovery_tokens,
    }


def _build_bank_statement_entry(
    statement: dict[str, Any],
    fallback: dict[str, Any],
    default_currency: Optional[str],
) -> dict[str, Any]:
    entry = copy.deepcopy(statement)

    for field_name in ("bank", "iban", "bic", "kontoinhaber"):
        if entry.get(field_name) is None and fallback.get(field_name) is not None:
            entry[field_name] = copy.deepcopy(fallback[field_name])

    bookings = _merge_bank_statement_bookings(entry.get("buchungen"), default_currency)
    entry["buchungen"] = bookings

    statement_number = _as_non_empty_string(entry.get("auszugsnummer"))
    if statement_number is None and isinstance(entry.get("_meta"), dict):
        statement_number = _as_non_empty_string(entry["_meta"].get("dokumenten_id"))
    if statement_number is not None:
        entry["auszugsnummer"] = statement_number

    if entry.get("datum") is None and bookings:
        entry["datum"] = bookings[-1].get("datum")
    if entry.get("endsaldo") is None and bookings:
        entry["endsaldo"] = bookings[-1].get("saldo")

    booking_dates = [_as_non_empty_string(item.get("datum")) for item in bookings]
    booking_dates = [item for item in booking_dates if item]
    if booking_dates:
        entry["zeitraum"] = {
            "von": booking_dates[0],
            "bis": booking_dates[-1],
        }

    currencies = {
        _as_non_empty_string(item.get("währung"))
        for item in bookings
        if isinstance(item, dict)
    }
    currencies.discard(None)
    if len(currencies) == 1:
        entry["währung"] = next(iter(currencies))
    elif default_currency and entry.get("währung") is None:
        entry["währung"] = default_currency

    return entry


def _synthesize_bank_statement_bundle_summary(extracted: dict[str, Any]) -> Optional[str]:
    statements = extracted.get("kontoauszuege")
    if not isinstance(statements, list) or not statements:
        return None

    statement_numbers: list[str] = []
    for statement in statements:
        if not isinstance(statement, dict):
            continue
        statement_number = _as_non_empty_string(statement.get("auszugsnummer"))
        if statement_number and statement_number not in statement_numbers:
            statement_numbers.append(statement_number)

    holder_name = None
    holder = extracted.get("kontoinhaber")
    if isinstance(holder, dict):
        holder_name = _as_non_empty_string(holder.get("name"))
    elif isinstance(holder, str):
        holder_name = _as_non_empty_string(holder)

    bank_name = _as_non_empty_string(extracted.get("bank"))
    date_period = extracted.get("zeitraum") if isinstance(extracted.get("zeitraum"), dict) else {}
    date_from = _as_non_empty_string(date_period.get("von"))
    date_to = _as_non_empty_string(date_period.get("bis"))
    booking_count = len(extracted.get("buchungen") or [])
    first_balance = _format_german_amount(extracted.get("anfangssaldo"))
    last_balance = _format_german_amount(extracted.get("endsaldo"))
    statement_label = ", ".join(statement_numbers) if statement_numbers else str(len(statements))

    subject_parts = ["Sammel-Kontoauszug"]
    if bank_name:
        subject_parts.append(f"der {bank_name}")
    if holder_name:
        subject_parts.append(f"für {holder_name}")

    summary = " ".join(subject_parts)
    summary += f" mit {len(statements)} Auszügen ({statement_label})"
    if booking_count:
        summary += f" und {booking_count} Buchungen"
    if date_from and date_to:
        summary += f" vom {date_from} bis {date_to}"
    if first_balance and last_balance:
        summary += f", Anfangssaldo {first_balance} EUR, Endsaldo {last_balance} EUR"
    summary += "."
    return summary


def _harmonize_bank_statement_bundle_fields(extracted: dict[str, Any]) -> dict[str, Any]:
    """Materialize a canonical bank-statement bundle shape without breaking legacy fields."""
    if not _looks_like_bank_statement(extracted):
        return extracted

    fallback = copy.deepcopy(extracted)
    top_level_currency = _as_non_empty_string(extracted.get("währung"))

    raw_statements = extracted.get("kontoauszuege")
    if isinstance(raw_statements, list) and raw_statements:
        statements = [
            _build_bank_statement_entry(item, fallback, top_level_currency)
            for item in raw_statements
            if isinstance(item, dict)
        ]
    else:
        statements = [_build_bank_statement_entry(fallback, fallback, top_level_currency)]

    if not statements:
        return extracted

    statement_numbers: list[str] = []
    merged_bookings: list[dict[str, Any]] = []
    booking_dates: list[str] = []
    detected_currencies: list[str] = []

    for statement in statements:
        statement_number = _as_non_empty_string(statement.get("auszugsnummer"))
        if statement_number and statement_number not in statement_numbers:
            statement_numbers.append(statement_number)

        statement_bookings = statement.get("buchungen") if isinstance(statement.get("buchungen"), list) else []
        for booking in statement_bookings:
            if not isinstance(booking, dict):
                continue
            merged_bookings.append(copy.deepcopy(booking))
            booking_date = _as_non_empty_string(booking.get("datum"))
            if booking_date:
                booking_dates.append(booking_date)
            booking_currency = _as_non_empty_string(booking.get("währung"))
            if booking_currency:
                detected_currencies.append(booking_currency)

    merged_bookings = _merge_bank_statement_bookings(merged_bookings, top_level_currency)
    booking_dates = sorted(booking_dates)
    overall_currency = None
    if detected_currencies and len(set(detected_currencies)) == 1:
        overall_currency = detected_currencies[0]

    extracted["kontoauszuege"] = statements
    extracted["buchungen"] = merged_bookings
    for shared_field in ("kontoinhaber", "iban", "bic", "bank"):
        if extracted.get(shared_field) is None and statements[0].get(shared_field) is not None:
            extracted[shared_field] = copy.deepcopy(statements[0][shared_field])
    if statement_numbers:
        extracted["auszugsnummer"] = ",".join(statement_numbers)
    extracted["anfangssaldo"] = statements[0].get("anfangssaldo")
    extracted["endsaldo"] = statements[-1].get("endsaldo")
    extracted["datum"] = statements[-1].get("datum")
    if overall_currency is not None:
        extracted["währung"] = overall_currency

    if booking_dates:
        extracted["zeitraum"] = {
            "von": booking_dates[0],
            "bis": booking_dates[-1],
        }

    meta_block = extracted.get("_meta")
    if not isinstance(meta_block, dict):
        meta_block = {}
        extracted["_meta"] = meta_block
    if statement_numbers:
        meta_block["dokumenten_id"] = ",".join(statement_numbers)

    if len(statements) > 1:
        summary = _synthesize_bank_statement_bundle_summary(extracted)
        if summary is not None:
            extracted["summary"] = summary
            meta_block["zusammenfassung"] = summary
            if "zusammenfassung" in extracted:
                extracted["zusammenfassung"] = summary

    return extracted


def _harmonize_extracted_summary_fields(extracted: dict[str, Any]) -> dict[str, Any]:
    """Normalize bundle semantics and summary fields onto canonical values."""
    extracted = _harmonize_bank_statement_bundle_fields(extracted)

    meta_block = extracted.get("_meta")
    should_materialize_meta_defaults = isinstance(meta_block, dict) or _looks_like_bank_statement(extracted)
    if should_materialize_meta_defaults:
        if not isinstance(meta_block, dict):
            meta_block = {}
            extracted["_meta"] = meta_block
        meta_block.setdefault("steuerrelevant", False)
        meta_block.setdefault("mwst_ausgewiesen", False)
    elif not isinstance(meta_block, dict):
        meta_block = None

    meta_summary = meta_block.get("zusammenfassung") if isinstance(meta_block, dict) else None

    canonical_summary = None
    for value in (extracted.get("summary"), extracted.get("zusammenfassung"), meta_summary):
        if isinstance(value, str) and value.strip():
            canonical_summary = value.strip()
            break

    if canonical_summary is None:
        return extracted

    extracted["summary"] = canonical_summary
    if not isinstance(meta_block, dict):
        meta_block = {}
        extracted["_meta"] = meta_block
    meta_block["zusammenfassung"] = canonical_summary
    if "zusammenfassung" in extracted:
        extracted["zusammenfassung"] = canonical_summary
    return extracted


def _load_steuer_signalwoerter() -> list[str]:
    """Load tax signal words from DB (prompt id='meta.steuer_signalwoerter')."""
    raw = _require_db_prompt("meta", "steuer_signalwoerter", language="de")
    return json.loads(raw)

_STEUER_SIGNALWOERTER: list[str] | None = None

def get_steuer_signalwoerter() -> list[str]:
    """Returns cached _STEUER_SIGNALWOERTER, loading from DB on first call."""
    global _STEUER_SIGNALWOERTER
    if _STEUER_SIGNALWOERTER is None:
        _STEUER_SIGNALWOERTER = _load_steuer_signalwoerter()
    return _STEUER_SIGNALWOERTER


def _load_datentyp_konventionen() -> str:
    """Load data type conventions from DB (prompt id='meta.datentyp_konventionen')."""
    return _require_db_prompt("meta", "datentyp_konventionen", language="de")

_DATENTYP_KONVENTIONEN: str | None = None

def get_datentyp_konventionen() -> str:
    """Returns cached _DATENTYP_KONVENTIONEN, loading from DB on first call."""
    global _DATENTYP_KONVENTIONEN
    if _DATENTYP_KONVENTIONEN is None:
        _DATENTYP_KONVENTIONEN = _load_datentyp_konventionen()
    return _DATENTYP_KONVENTIONEN


# =============================================================================
# DB Helpers — importiert aus templates_db (T-DAI-034)
# =============================================================================

from templates_db import get_db_connection, get_template_by_id  # noqa: E402


# =============================================================================
# Template Registry — Kategorien
# =============================================================================

def get_classify_categories_from_db() -> list[str]:
    """Lädt alle Template-IDs + display_names aus der DB für den Classify-Prompt.

    Returns:
        Liste von Strings im Format "id: display_name", z.B. ["invoice: Rechnung", ...].

    Raises:
        RuntimeError: Wenn keine aktivierten Templates vorhanden sind.
    """
    now = time.time()
    cache = _get("_classify_categories_cache", _classify_categories_cache)
    ttl = _get("_CLASSIFY_CACHE_TTL", _CLASSIFY_CACHE_TTL)
    if cache["categories"] is not None and (now - cache["timestamp"]) < ttl:
        return cache["categories"]

    try:
        from templates_db import _return_conn
        _get_db_connection = _get("get_db_connection", get_db_connection)
        conn = _get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, display_name FROM template WHERE enabled = 1 ORDER BY priority DESC, id"
            )
            rows = cur.fetchall()
        finally:
            _return_conn(conn)
        if not rows:
            raise RuntimeError("Keine aktivierten Templates für Dokumentklassifizierung konfiguriert")
        categories = [f"{r['id']}: {r['display_name']}" for r in rows]
        cache["categories"] = categories
        cache["timestamp"] = now
        return categories
    except Exception:
        raise


# =============================================================================
# Dual-Pass Vision-Validierung
# =============================================================================

async def dual_pass_validate(
    markdown: str,
    file_data: bytes,
    mimetype: str,
    language: str = "de",
) -> str:
    """
    Validiert und korrigiert OCR-extrahierten Markdown via Dual-Pass Vision-Vergleich.

    Schickt den OCR-extrahierten Markdown-Text zusammen mit dem Originalbild an die
    Vision-API. Das Modell vergleicht beides und korrigiert Fehler in Struktur,
    Tabellen-Spalten und Inhalt.

    Bei fehlender Vision-API oder Fehler wird der Original-Markdown zurückgegeben
    (graceful degradation).

    Args:
        markdown: Per OCR extrahierter Markdown-Text.
        file_data: Rohe Bytes der Originaldatei (Bild oder PDF-Seite als Bild).
        mimetype: MIME-Type der Datei (z.B. 'image/png', 'image/jpeg').
        language: Sprache für den Vision-Prompt (Standard: "de").

    Returns:
        Korrigierter Markdown-Text oder Original bei Fehler.
    """
    _api_key = _get("MISTRAL_API_KEY", MISTRAL_API_KEY)
    _analyze_vision = _get("analyze_with_mistral_vision", analyze_with_mistral_vision)
    if not _api_key:
        log.warning("dual_pass_validate_skipped_no_api_key")
        return markdown

    prompt = _render_prompt_template(
        _require_db_prompt("validate", "dual_pass", language="de"),
        markdown=markdown,
    )

    log.info("dual_pass_validate_start", mimetype=mimetype, markdown_len=len(markdown))

    try:
        result = await _analyze_vision(
            image_data=file_data,
            mimetype=mimetype,
            prompt=prompt,
            language=language,
        )

        if result.get("success"):
            corrected = result.get("markdown", markdown)
            log.info("dual_pass_validate_done", original_len=len(markdown), corrected_len=len(corrected))
            return corrected
        else:
            log.warning(
                "dual_pass_validate_vision_failed",
                error=result.get("error", "unknown"),
            )
            return markdown

    except Exception as exc:
        log.warning("dual_pass_validate_exception", error=str(exc), exc_info=True)
        return markdown


# =============================================================================
# Dokumenten-Klassifizierung via LLM
# =============================================================================

async def classify_document(
    markdown: str,
    categories: list[str] | None = None,
    language: str = "de",
    request_id: Optional[str] = None,
    attempt_number: Optional[int] = None,
) -> dict[str, Any]:
    """
    Klassifiziert ein Dokument anhand seines Markdown-Inhalts via Mistral API.

    Args:
        markdown: Konvertierter Markdown-Text des Dokuments.
        categories: Erlaubte Dokumenttypen. Wenn None, werden DEFAULT_CLASSIFY_CATEGORIES
                    verwendet.
        language: Sprache des Prompts ('de' oder 'en').

    Returns:
        Dict mit 'document_type' (str) und 'document_type_confidence' (float 0.0–1.0).
        Bei Fehlern wird {"document_type": "other", "document_type_confidence": 0.0}
        zurückgegeben (graceful degradation).
    """
    _api_key = _get("MISTRAL_API_KEY", MISTRAL_API_KEY)
    _text_model = _get("MISTRAL_TEXT_MODEL", MISTRAL_TEXT_MODEL)
    _classify_max_tokens = _get("CLASSIFY_MAX_TOKENS", CLASSIFY_MAX_TOKENS)
    _classify_max_chars = _get("CLASSIFY_MAX_CHARS", CLASSIFY_MAX_CHARS)
    _call_api = _get("call_mistral_vision_api", call_mistral_vision_api)
    if not _api_key:
        log.warning("classify_document_no_api_key")
        return {"document_type": "other", "document_type_confidence": 0.0}

    # Template-Registry Kategorien laden (statt hardcodierte Liste)
    if categories is not None:
        # Explizite Kategorien vom Aufrufer → direkt als "id: id" nutzen (Rückwärtskompatibilität)
        db_categories = [f"{c}: {c}" for c in categories]
        # Erlaubte IDs für Validierung
        allowed_ids = list(categories)
    else:
        # Aus Template-Registry laden
        db_categories = get_classify_categories_from_db()
        # Erlaubte IDs aus "id: display_name" extrahieren
        allowed_ids = [c.split(":")[0].strip() for c in db_categories]

    # Kategorien-String für den Prompt: "id: Beschreibung" zeilenweise
    categories_lines = "\n".join(db_categories)

    # Markdown auf maximal _classify_max_chars Zeichen kürzen, um Token-Kosten zu begrenzen
    truncated_markdown = markdown[:_classify_max_chars] if len(markdown) > _classify_max_chars else markdown

    if language == "de":
        system_prompt = _require_db_prompt("classify", "system_de", language="de")
        user_prompt = _render_prompt_template(
            _require_db_prompt("classify", "user_de", language="de"),
            categories_lines=categories_lines,
            truncated_markdown=truncated_markdown,
        )
    else:
        system_prompt = _require_db_prompt("classify", "system_en", language="en")
        user_prompt = _render_prompt_template(
            _require_db_prompt("classify", "user_en", language="en"),
            categories_lines=categories_lines,
            truncated_markdown=truncated_markdown,
        )

    payload = {
        "model": _text_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": _classify_max_tokens,
        "temperature": 0.0,
    }

    try:
        log.info(
            "classify_document_start",
            categories=allowed_ids,
            text_length=len(truncated_markdown),
            request_id=request_id,
            attempt_number=attempt_number,
        )
        result = await _call_api(
            payload,
            request_id=request_id,
            attempt_number=attempt_number,
            pipeline_step="classify_document",
        )
        content = result["choices"][0]["message"]["content"].strip()

        # JSON aus der Antwort extrahieren (Modell könnte Markdown-Code-Blöcke liefern)
        json_match = re.search(r"\{[^}]+\}", content, re.DOTALL)
        if not json_match:
            log.warning("classify_document_no_json", raw_content=content)
            return {"document_type": "other", "document_type_confidence": 0.0}

        parsed = json.loads(json_match.group(0))
        doc_type = str(parsed.get("type", "other")).strip()
        confidence = float(parsed.get("confidence", 0.0))

        # Nur erlaubte Typen (IDs) durchlassen
        if doc_type not in allowed_ids:
            log.warning("classify_document_unknown_type", doc_type=doc_type, allowed=allowed_ids)
            doc_type = "other"

        # Konfidenz auf gültigen Bereich begrenzen
        confidence = max(0.0, min(1.0, confidence))

        log.info(
            "classify_document_done",
            document_type=doc_type,
            confidence=confidence,
            request_id=request_id,
            attempt_number=attempt_number,
        )
        return {"document_type": doc_type, "document_type_confidence": confidence}

    except json.JSONDecodeError as exc:
        log.warning("classify_document_json_error", error=str(exc), request_id=request_id, attempt_number=attempt_number)
        return {"document_type": "other", "document_type_confidence": 0.0}
    except Exception as exc:
        log.warning("classify_document_api_error", error=str(exc), request_id=request_id, attempt_number=attempt_number, exc_info=True)
        return {"document_type": "other", "document_type_confidence": 0.0}


# =============================================================================
# OCR-Nachkorrektur via LLM
# =============================================================================

async def correct_ocr_text(
    text: str,
    language: str = "de",
    request_id: Optional[str] = None,
    attempt_number: Optional[int] = None,
) -> dict[str, Any]:
    """
    Korrigiert typische OCR-Fehler in einem Text via Mistral API.

    Sendet den OCR-Text mit einem speziellen Korrektur-Prompt an die Mistral API.
    Typische OCR-Artefakte wie Zeichenverwechslungen (rn→m, 0→O, l→1, fi→fi),
    falsche Worttrennungen und fehlende Leerzeichen werden behoben.
    Inhaltliche Fakten, Zahlen, Namen und Daten werden NICHT verändert.

    Args:
        text: OCR-extrahierter Text (Markdown).
        language: Sprache des Textes ('de' oder 'en').

    Returns:
        Dict mit:
        - corrected_text (str): Korrigierter Text
        - corrections_count (int): Anzahl der Korrekturen
        - tokens (int): Verbrauchte Tokens
        - success (bool): War die Korrektur erfolgreich?
        - error (str, optional): Fehlermeldung bei Misserfolg
    """
    _api_key = _get("MISTRAL_API_KEY", MISTRAL_API_KEY)
    _text_model = _get("MISTRAL_TEXT_MODEL", MISTRAL_TEXT_MODEL)
    _ocr_max_tokens = _get("OCR_CORRECT_MAX_TOKENS", OCR_CORRECT_MAX_TOKENS)
    _call_api = _get("call_mistral_vision_api", call_mistral_vision_api)
    _strip = _get("strip_llm_artifacts", strip_llm_artifacts)
    if not _api_key:
        log.warning("correct_ocr_text_no_api_key")
        return {
            "success": False,
            "error": "MISTRAL_API_KEY nicht konfiguriert",
            "corrected_text": text,
            "corrections_count": 0,
            "tokens": 0,
        }

    if language == "de":
        system_prompt = _require_db_prompt("ocr_correct", "system_de", language="de")
        user_prompt = _render_prompt_template(
            _require_db_prompt("ocr_correct", "user_de", language="de"),
            text=text,
        )
    else:
        system_prompt = _require_db_prompt("ocr_correct", "system_en", language="en")
        user_prompt = _render_prompt_template(
            _require_db_prompt("ocr_correct", "user_en", language="en"),
            text=text,
        )

    payload = {
        "model": _text_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": _ocr_max_tokens,
        "temperature": 0.0,
    }

    try:
        log.info(
            "correct_ocr_text_start",
            text_length=len(text),
            language=language,
            request_id=request_id,
            attempt_number=attempt_number,
        )
        result = await _call_api(
            payload,
            request_id=request_id,
            attempt_number=attempt_number,
            pipeline_step="correct_ocr_text",
        )
        content = result["choices"][0]["message"]["content"]
        usage = result.get("usage", {})
        tokens_total = usage.get("total_tokens", 0)

        # Korrektur-Anzahl aus dem speziellen Marker parsen
        corrections_count = 0
        corrected_text = content

        marker_match = re.search(r"<<<CORRECTIONS:(\d+)>>>\s*$", content.strip(), re.MULTILINE)
        if not marker_match:
            # Fallback: altes Marker-Format unterstützen
            marker_match = re.search(r"---CORRECTIONS:\s*(\d+)\s*$", content.strip(), re.MULTILINE)
        if marker_match:
            corrections_count = int(marker_match.group(1))
            # Marker aus dem Text entfernen
            corrected_text = _strip(content[: marker_match.start()].rstrip())
        else:
            log.warning("correct_ocr_text_no_corrections_marker", content_tail=content[-100:])
            corrected_text = _strip(content)

        log.info(
            "correct_ocr_text_done",
            corrections_count=corrections_count,
            tokens=tokens_total,
            request_id=request_id,
            attempt_number=attempt_number,
        )
        return {
            "success": True,
            "corrected_text": corrected_text,
            "corrections_count": corrections_count,
            "tokens": tokens_total,
        }

    except Exception as exc:
        log.warning("correct_ocr_text_api_error", error=str(exc), request_id=request_id, attempt_number=attempt_number, exc_info=True)
        return {
            "success": False,
            "error": str(exc),
            "corrected_text": text,
            "corrections_count": 0,
            "tokens": 0,
        }


# =============================================================================
# Schema-basierte strukturierte Extraktion
# =============================================================================

def _make_null_tolerant(schema: dict) -> dict:
    """Macht ein JSON-Schema null-tolerant: 'type': 'string' → 'type': ['string', 'null'].

    LLMs geben für fehlende Felder korrekt null zurück. Standard JSON-Schema lehnt das ab
    wenn der Typ nur 'string' ist. Diese Funktion patcht rekursiv alle primitiven Typ-Deklarationen.
    """
    s = copy.deepcopy(schema)

    def _patch(obj: dict) -> None:
        if not isinstance(obj, dict):
            return
        if "type" in obj and isinstance(obj["type"], str) and obj["type"] != "object" and obj["type"] != "array":
            obj["type"] = [obj["type"], "null"]
        if "properties" in obj and isinstance(obj["properties"], dict):
            for prop in obj["properties"].values():
                _patch(prop)
        if "items" in obj and isinstance(obj["items"], dict):
            _patch(obj["items"])

    _patch(s)
    return s


async def extract_structured_data(
    markdown: str,
    schema: dict,
    language: str = "de",
    field_descriptions: Optional[dict | str] = None,
    notes: Optional[str] = None,
    request_id: Optional[str] = None,
    attempt_number: Optional[int] = None,
    enable_bank_bundle_recovery: bool = True,
    strict_schema_validation: bool = True,
) -> dict:
    """
    Extrahiert strukturierte Daten aus Markdown gemäß einem JSON-Schema via Mistral API.

    Sendet den Markdown-Inhalt zusammen mit dem Schema an die Mistral API.
    Die Antwort wird als JSON geparst und optional gegen das Schema validiert.

    Args:
        markdown: Konvertierter Markdown-Text des Dokuments.
        schema: JSON-Schema für die gewünschten extrahierten Felder.
        language: Sprache des Prompts ('de' oder 'en').
        field_descriptions: Kontext/Beschreibung pro Feld aus der Template-DB (optional).
        notes: Zusätzliche Hinweise aus der Template-DB (optional).

    Returns:
        Dict mit:
        - success (bool)
        - extracted (dict): Extrahierte Daten passend zum Schema
        - tokens (int): Verbrauchte Tokens
        - error (str): Fehlermeldung (nur bei success=False)
    """
    _api_key = _get("MISTRAL_API_KEY", MISTRAL_API_KEY)
    _text_model = _get("MISTRAL_TEXT_MODEL", MISTRAL_TEXT_MODEL)
    _extract_max_tokens = _get("EXTRACT_MAX_TOKENS", EXTRACT_MAX_TOKENS)
    _extract_max_chars = _get("EXTRACT_MAX_CHARS", EXTRACT_MAX_CHARS)
    _extract_response_format = _get("MISTRAL_EXTRACT_RESPONSE_FORMAT", MISTRAL_EXTRACT_RESPONSE_FORMAT)
    _call_api = _get("call_mistral_vision_api", call_mistral_vision_api)
    _jsonschema_available = _get("JSONSCHEMA_AVAILABLE", JSONSCHEMA_AVAILABLE)
    if not _api_key:
        log.warning("extract_structured_data_no_api_key")
        return {
            "success": False,
            "error": "MISTRAL_API_KEY nicht konfiguriert",
            "extracted": None,
            "tokens": 0,
        }

    schema_str = json.dumps(schema, ensure_ascii=False)

    # Template-spezifische Hints aus der DB
    template_hints = ""
    if field_descriptions:
        fd = field_descriptions if isinstance(field_descriptions, str) else json.dumps(field_descriptions, indent=2, ensure_ascii=False)
        template_hints += f"\n\nHinweise zu den Feldern:\n{fd}"
    if notes:
        template_hints += f"\n\nZusätzliche Hinweise:\n{notes}"

    # _meta Block Instruktion — wird an beide Sprach-Prompts angehängt
    meta_instruction = (
        "\n\nZUSÄTZLICH zu den Schema-Feldern extrahiere IMMER einen '_meta' Block mit folgenden Feldern:\n"
        + json.dumps(get_meta_schema(), indent=2, ensure_ascii=False)
        + "\n\nAbsender/Empfänger-Regeln:"
        + "\n- absender: Wer hat das Dokument erstellt/verschickt? Bei Firmen: firma='Telekom Deutschland GmbH', name=null. Bei Personen mit Firma: name='Thomas Weber', firma='Schornsteinfeger Weber'. WICHTIG: firma ist IMMER der offizielle Firmenname aus Briefkopf/Impressum — NICHT der Kundenberater oder Sachbearbeiter. Beispiel: 'Ihr Ansprechpartner: Sherife Berisha, IONOS SE' → firma='IONOS SE', name='Sherife Berisha'."
        + "\n- empfaenger: An wen ist das Dokument gerichtet? Bei Kassenbons ohne Empfänger: name=null."
        + "\n- Adresse nur befüllen wenn im Dokument erkennbar."
        + "\n- slug: Der geläufigste, kürzeste Name unter dem man den Absender/Empfänger kennt. "
        + "Wie ein Mensch ihn im Alltag nennen würde. Kleinbuchstaben, Bindestriche statt Leerzeichen, "
        + "keine Rechtsformen (GmbH, AG, e.V.), keine Titel (Dr., Dipl.-Ing.), "
        + "keine Abteilungen (Amt für...) — nur der Kern. "
        + "Beispiele: 'Telekom Deutschland GmbH' → 'telekom', "
        + "'Debeka Krankenversicherungsverein a. G.' → 'debeka', "
        + "'Landeshauptstadt Düsseldorf, Amt für Zentrale Dienste, CC Beihilfe' → 'beihilfe', "
        + "'Landesamt für Besoldung und Versorgung NRW' → 'lbv', "
        + "'Thomas Weber, Bevollmächtigter Bezirksschornsteinfeger' → 'weber', "
        + "'Dr. med. Anna Schmidt' → 'dr-schmidt', "
        + "'REWE Sascha Sieger oHG' → 'rewe', "
        + "'Deutsche Rentenversicherung' → 'rentenversicherung', "
        + "'Stadtsparkasse Mönchengladbach' → 'sparkasse-mg', "
        + "'ING-DiBa AG' → 'ing'."
        + "\n\nSteuerrelevanz-Signalwörter (aktiv suchen): "
        + ", ".join(get_steuer_signalwoerter()[:10]) + ", ..."
        + "\nAuch implizit steuerrelevant: Rechnungen mit MwSt, Gehaltsabrechnungen, Versicherungsbeiträge, Handwerkerleistungen."
        + "\n\nZusammenfassungs-Regeln:"
        + "\n- Wenn das Schema `summary` oder `zusammenfassung` enthält, dann fasse den gesamten Dokumentinhalt zusammen, nicht nur eine einzelne Seite oder einen Datumsbereich."
        + "\n- `_meta.zusammenfassung` ist die kanonische Kurz-Zusammenfassung des gesamten Dokuments."
        + "\n- Wenn sowohl `summary`/`zusammenfassung` als auch `_meta.zusammenfassung` vorkommen, müssen sie inhaltlich übereinstimmen."
        + "\n" + get_datentyp_konventionen()
    )

    _schema_str = json.dumps(schema, indent=2, ensure_ascii=False)
    _doc_truncated = markdown[:_extract_max_chars]
    if language == "de":
        system_prompt = _require_db_prompt("extract", "system_de", language="de")
        user_prompt = _render_prompt_template(
            _require_db_prompt("extract", "user_de", language="de"),
            schema_str=_schema_str,
            template_hints=template_hints,
            meta_instruction=meta_instruction,
            markdown=_doc_truncated,
        )
    else:
        system_prompt = _require_db_prompt("extract", "system_en", language="en")
        user_prompt = _render_prompt_template(
            _require_db_prompt("extract", "user_en", language="en"),
            schema_str=_schema_str,
            template_hints=template_hints,
            meta_instruction=meta_instruction,
            markdown=_doc_truncated,
        )

    payload = {
        "model": _text_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": _extract_max_tokens,
        "temperature": 0.0,
    }
    if _extract_response_format == "json_object":
        payload["response_format"] = {"type": "json_object"}
    elif _extract_response_format == "json_schema":
        structured_schema = copy.deepcopy(schema)
        structured_schema.setdefault("type", "object")
        structured_schema.setdefault("properties", {})
        structured_schema["properties"]["_meta"] = _meta_description_to_json_schema(get_meta_schema())
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "daigestr_extraction",
                "schema": structured_schema,
            },
        }

    try:
        log.info(
            "extract_structured_data_start",
            schema_keys=list(schema.get("properties", {}).keys()),
            text_length=len(markdown),
            response_format=_extract_response_format,
            request_id=request_id,
            attempt_number=attempt_number,
        )
        result = await _call_api(
            payload,
            request_id=request_id,
            attempt_number=attempt_number,
            pipeline_step="extract_structured_data",
        )
        content = result["choices"][0]["message"]["content"]
        usage = result.get("usage", {})
        tokens = usage.get("total_tokens", 0)

        if isinstance(content, dict):
            extracted = _harmonize_extracted_summary_fields(content)
        else:
            content_str = content.strip() if isinstance(content, str) else str(content)
            json_match = re.search(r"\{[\s\S]*\}", content_str)
            if not json_match:
                log.warning("extract_structured_data_no_json", raw_content=content_str[:200], request_id=request_id, attempt_number=attempt_number)
                return {
                    "success": False,
                    "error": f"Kein JSON in der API-Antwort gefunden: {content_str[:100]}",
                    "extracted": None,
                    "tokens": tokens,
                }

            extracted = _harmonize_extracted_summary_fields(json.loads(json_match.group(0)))

        if enable_bank_bundle_recovery and _needs_bank_statement_bundle_recovery(markdown, extracted):
            recovery_result = await _recover_bank_statement_bundle(
                markdown,
                schema,
                language=language,
                field_descriptions=field_descriptions,
                notes=notes,
                request_id=request_id,
                attempt_number=attempt_number,
            )
            if recovery_result is not None:
                extracted = recovery_result["extracted"]
                tokens += int(recovery_result.get("tokens") or 0)

        if not _has_meaningful_extracted_payload(extracted):
            log.warning(
                "extract_structured_data_empty_payload",
                request_id=request_id,
                attempt_number=attempt_number,
            )
            return {
                "success": False,
                "error": "Strukturierte Extraktion lieferte keine verwertbaren Felder",
                "extracted": extracted,
                "tokens": tokens,
            }

        # Schema-Validierung (AC-014-7) — null-tolerant
        if _jsonschema_available:
            try:
                # Schema null-tolerant machen: "type": "string" → "type": ["string", "null"]
                tolerant_schema = _make_null_tolerant(schema)
                jsonschema.validate(instance=extracted, schema=tolerant_schema)
                log.info("extract_structured_data_valid", tokens=tokens, request_id=request_id, attempt_number=attempt_number)
            except jsonschema.ValidationError as ve:
                log.warning(
                    "extract_structured_data_schema_violation",
                    error=str(ve.message),
                    tokens=tokens,
                    request_id=request_id,
                    attempt_number=attempt_number,
                )
                if strict_schema_validation:
                    return {
                        "success": False,
                        "error": f"Schema-Verletzung in strukturierter Extraktion: {ve.message}",
                        "extracted": extracted,
                        "tokens": tokens,
                    }
        else:
            log.debug("extract_structured_data_no_jsonschema")

        log.info("extract_structured_data_success", tokens=tokens, request_id=request_id, attempt_number=attempt_number)
        return {
            "success": True,
            "extracted": extracted,
            "tokens": tokens,
        }

    except json.JSONDecodeError as exc:
        if enable_bank_bundle_recovery:
            recovery_result = await _recover_bank_statement_bundle(
                markdown,
                schema,
                language=language,
                field_descriptions=field_descriptions,
                notes=notes,
                request_id=request_id,
                attempt_number=attempt_number,
            )
            if recovery_result is not None:
                return recovery_result
        log.warning("extract_structured_data_json_decode_error", error=str(exc), request_id=request_id, attempt_number=attempt_number)
        return {
            "success": False,
            "error": f"JSON-Parsing fehlgeschlagen: {str(exc)}",
            "extracted": None,
            "tokens": 0,
        }
    except Exception as exc:
        if enable_bank_bundle_recovery:
            recovery_result = await _recover_bank_statement_bundle(
                markdown,
                schema,
                language=language,
                field_descriptions=field_descriptions,
                notes=notes,
                request_id=request_id,
                attempt_number=attempt_number,
            )
            if recovery_result is not None:
                return recovery_result
        log.error("extract_structured_data_api_error", error=str(exc), request_id=request_id, attempt_number=attempt_number, exc_info=True)
        return {
            "success": False,
            "error": f"API-Fehler bei Extraktion: {str(exc)}",
            "extracted": None,
            "tokens": 0,
        }


# =============================================================================
# Quality Scoring (FR-MKIT-010)
# =============================================================================

def calculate_quality_score(markdown: str, meta: dict) -> dict:
    """
    Berechnet einen Qualitäts-Score für konvertierten Markdown-Text.

    Scoring-Komponenten:
    - Zeichendichte (0-0.3): Verhältnis sinnvoller Zeichen zu Whitespace
    - Wort-Qualität (0-0.3): Anteil erkennbarer Wörter (>= 3 Buchstaben)
    - Struktur-Elemente (0-0.2): Headings, Listen, Tabellen, Code-Blöcke
    - OCR/Vision Confidence (0-0.2): Token-Effizienz als Proxy bei vision_used

    Args:
        markdown: Konvertierter Markdown-Text
        meta: Metadaten-Dictionary (kann vision_used, tokens_* enthalten)

    Returns:
        Dict mit quality_score (0.0-1.0) und quality_grade ('poor'/'fair'/'good'/'excellent')
    """
    if not markdown or not markdown.strip():
        return {"quality_score": 0.0, "quality_grade": "poor"}

    # Scoring-Gewichtungen aus DB laden (mit Fallback auf Hardcode)
    from templates_db import get_scoring_weight as _get_sw  # noqa: PLC0415 — lazy import
    _gsw = _get("get_scoring_weight", _get_sw)

    def _sw(name: str, default: float) -> float:
        try:
            return _gsw(name)
        except Exception:
            return default

    _density_low = _sw("density_low_threshold", 0.1)
    _density_mid = _sw("density_mid_threshold", 0.2)
    _density_high = _sw("density_high_threshold", 0.8)
    _density_optimal = _sw("density_optimal", 0.3)
    _word_max = _sw("word_quality_max", 0.3)
    _word_min_chars = int(_sw("word_min_chars_threshold", 50))
    _word_short_penalty = _sw("word_short_text_penalty", 0.5)
    _struct_elem_score = _sw("structure_element_score", 0.05)
    _struct_max = _sw("structure_max", 0.2)
    _vision_eff_high = _sw("vision_efficiency_high", 0.5)
    _vision_eff_mid = _sw("vision_efficiency_mid", 0.2)
    _vision_eff_low = _sw("vision_efficiency_low", 0.05)
    _vision_sc_high = _sw("vision_score_high", 0.2)
    _vision_sc_mid = _sw("vision_score_mid", 0.15)
    _vision_sc_low = _sw("vision_score_low", 0.1)
    _vision_sc_min = _sw("vision_score_min", 0.05)
    _vision_baseline = _sw("vision_baseline", 0.2)
    _grade_poor = _sw("grade_poor", 0.3)
    _grade_fair = _sw("grade_fair", 0.6)
    _grade_good = _sw("grade_good", 0.8)

    text = markdown.strip()
    total_chars = len(text)

    # --- Komponente 1: Zeichendichte (0-0.3) ---
    # Verhältnis von Nicht-Whitespace zu Gesamt-Zeichen
    non_ws_chars = len(re.sub(r'\s', '', text))
    if total_chars > 0:
        density_ratio = non_ws_chars / total_chars
        # Gute Dichte ist 0.4-0.8; sehr niedrig (<0.2) oder sehr hoch (>0.9) ist suspicious
        if density_ratio < _density_low:
            density_score = 0.0
        elif density_ratio < _density_mid:
            density_score = density_ratio * 1.0  # linear bis _density_mid
        elif density_ratio <= _density_high:
            density_score = _density_optimal  # Optimal
        else:
            density_score = max(0.1, _density_optimal * (1.0 - density_ratio))
    else:
        density_score = 0.0

    # --- Komponente 2: Wort-Qualität (0-0.3) ---
    # Anteil erkennbarer Wörter (mindestens 3 Buchstaben, keine Gibberish-Sequenzen)
    words = re.findall(r'[a-zA-ZäöüÄÖÜß]{3,}', text)
    total_word_tokens = re.findall(r'\S+', text)

    if total_word_tokens:
        word_ratio = len(words) / len(total_word_tokens)
        word_score = min(_word_max, word_ratio * _word_max)
    else:
        word_score = 0.0

    # Bonus: Mindestlänge — sehr kurzer Text bekommt Abzug
    if total_chars < _word_min_chars:
        word_score *= _word_short_penalty

    # --- Komponente 3: Struktur-Elemente (0-0.2) ---
    structure_score = 0.0
    lines = text.split('\n')

    has_headings = any(line.strip().startswith('#') for line in lines)
    has_lists = any(re.match(r'^\s*[-*+]\s', line) or re.match(r'^\s*\d+\.\s', line) for line in lines)
    has_tables = any('|' in line and line.count('|') >= 2 for line in lines)
    has_codeblocks = '```' in text

    structure_elements = sum([has_headings, has_lists, has_tables, has_codeblocks])
    structure_score = min(_struct_max, structure_elements * _struct_elem_score)

    # --- Komponente 4: OCR/Vision Confidence (0-0.2) ---
    vision_score = 0.0
    vision_used = meta.get("vision_used", False)
    scanned = meta.get("scanned", False)

    if vision_used or scanned:
        ocr_average_page_confidence = meta.get("ocr_average_page_confidence")
        ocr_minimum_page_confidence = meta.get("ocr_minimum_page_confidence")
        if isinstance(ocr_average_page_confidence, (int, float)):
            effective_confidence = float(ocr_average_page_confidence)
            if isinstance(ocr_minimum_page_confidence, (int, float)):
                effective_confidence = min(effective_confidence, float(ocr_minimum_page_confidence))
            if effective_confidence >= 0.95:
                vision_score = _vision_sc_high
            elif effective_confidence >= 0.85:
                vision_score = _vision_sc_mid
            elif effective_confidence >= 0.70:
                vision_score = _vision_sc_low
            else:
                vision_score = _vision_sc_min
        else:
            tokens_prompt = meta.get("tokens_prompt") or 0
            tokens_completion = meta.get("tokens_completion") or 0

            if tokens_prompt > 0 and tokens_completion > 0:
                # Token-Effizienz: mehr Output-Tokens relativ zu Input → guter Inhalt extrahiert
                efficiency = tokens_completion / tokens_prompt
                if efficiency >= _vision_eff_high:
                    vision_score = _vision_sc_high
                elif efficiency >= _vision_eff_mid:
                    vision_score = _vision_sc_mid
                elif efficiency >= _vision_eff_low:
                    vision_score = _vision_sc_low
                else:
                    vision_score = _vision_sc_min
            elif tokens_completion > 0:
                # Nur Completion bekannt → Mindest-Score
                vision_score = _vision_sc_low
            else:
                # Vision wurde verwendet, aber keine Token-Daten → neutraler Wert
                vision_score = _vision_sc_low
    else:
        # Kein Vision → volle _vision_baseline als Baseline (keine Unsicherheit durch OCR)
        vision_score = _vision_baseline

    # --- Gesamt-Score ---
    raw_score = density_score + word_score + structure_score + vision_score
    quality_score = round(min(1.0, max(0.0, raw_score)), 4)

    # --- Grade Mapping ---
    if quality_score < _grade_poor:
        quality_grade = "poor"
    elif quality_score < _grade_fair:
        quality_grade = "fair"
    elif quality_score < _grade_good:
        quality_grade = "good"
    else:
        quality_grade = "excellent"

    log.debug(
        "quality_score_calculated",
        score=quality_score,
        grade=quality_grade,
        density=density_score,
        words=word_score,
        structure=structure_score,
        vision=vision_score,
    )

    return {"quality_score": quality_score, "quality_grade": quality_grade}


# =============================================================================
# Smart Chunking (FR-MKIT-011)
# =============================================================================

def chunk_markdown(markdown: str, chunk_size: int = 512, source: str = "") -> list[dict]:
    """
    Splittet Markdown intelligent an Heading-Grenzen für RAG-Anwendungen.

    Algorithmus:
    1. Identifiziert alle Headings (# ## ### etc.) mit ihren Positionen
    2. Schützt "atomare" Blöcke: Tabellen (| ... |) und Code-Blöcke (``` ... ```)
    3. Splittet an Headings; wenn ein Chunk > chunk_size Tokens, werden Absätze
       (doppelte Newlines) als sekundäre Split-Punkte genutzt
    4. Tabellen und Code-Blöcke werden niemals zerstückelt

    Args:
        markdown: Der zu chunkende Markdown-Text
        chunk_size: Maximale Chunk-Größe in Tokens (Heuristik: len(text) / 4)
        source: Quelldatei-Name (wird in Chunk-Metadaten eingebettet)

    Returns:
        Liste von Chunk-Dicts mit: index, heading, source, token_count, text
    """
    if not markdown or not markdown.strip():
        return []

    def _token_count(text: str) -> int:
        """Heuristische Token-Schätzung: len / 4."""
        return max(1, len(text) // 4)

    def _split_into_sections(text: str) -> list[tuple[str, str]]:
        """
        Teilt Text in (heading, content)-Paare auf.
        Heading ist leer für Inhalt vor dem ersten Heading.
        Atomare Blöcke (Code/Tabellen) werden nicht zerstückelt.
        """
        lines = text.split("\n")
        sections: list[tuple[str, str]] = []
        current_heading = ""
        current_lines: list[str] = []
        in_code_block = False
        in_table = False

        for line in lines:
            stripped = line.strip()

            # Code-Block tracking
            if stripped.startswith("```"):
                if in_code_block:
                    # Ende des Code-Blocks
                    current_lines.append(line)
                    in_code_block = False
                    continue
                else:
                    in_code_block = True
                    current_lines.append(line)
                    continue

            if in_code_block:
                current_lines.append(line)
                continue

            # Tabellen-Tracking (Zeilen die mit | anfangen und enden)
            if stripped.startswith("|") and stripped.endswith("|"):
                in_table = True
                current_lines.append(line)
                continue
            else:
                in_table = False

            # Heading-Erkennung (nur außerhalb atomarer Blöcke)
            heading_match = re.match(r"^(#{1,6})\s+(.+)$", line)
            if heading_match:
                # Speichert bisherigen Abschnitt
                if current_lines or current_heading:
                    sections.append((current_heading, "\n".join(current_lines)))
                current_heading = line.strip()
                current_lines = []
            else:
                current_lines.append(line)

        # Letzten Abschnitt speichern
        if current_lines or current_heading:
            sections.append((current_heading, "\n".join(current_lines)))

        return sections

    def _split_at_paragraphs(heading: str, content: str, chunk_size: int) -> list[tuple[str, str]]:
        """
        Splittet langen Inhalt an Absatz-Grenzen (doppelte Newlines).
        Respektiert Code-Blöcke und Tabellen als atomare Einheiten.
        """
        # Wenn unter Größenlimit: direkt zurückgeben
        full_text = (heading + "\n\n" + content).strip() if heading else content.strip()
        if _token_count(full_text) <= chunk_size:
            return [(heading, content)]

        # Teile an doppelten Newlines auf, aber behalte Code/Tabellen zusammen
        paragraphs: list[str] = []
        current_para: list[str] = []
        in_code = False
        in_table = False

        for line in content.split("\n"):
            stripped = line.strip()

            if stripped.startswith("```"):
                if in_code:
                    current_para.append(line)
                    in_code = False
                else:
                    in_code = True
                    current_para.append(line)
                continue

            if in_code:
                current_para.append(line)
                continue

            if stripped.startswith("|") and stripped.endswith("|"):
                in_table = True
                current_para.append(line)
                continue
            else:
                if in_table and stripped == "":
                    # Ende der Tabelle: speichern als atomaren Absatz
                    paragraphs.append("\n".join(current_para))
                    current_para = []
                    in_table = False
                    continue
                in_table = False

            if stripped == "" and current_para and not in_code and not in_table:
                # Paragraph-Grenze
                paragraphs.append("\n".join(current_para))
                current_para = []
            else:
                current_para.append(line)

        if current_para:
            paragraphs.append("\n".join(current_para))

        # Paragraphen zu Chunks zusammenfassen (so viel wie möglich unter chunk_size)
        result_sections: list[tuple[str, str]] = []
        current_chunk_lines: list[str] = []
        current_tokens = _token_count(heading) if heading else 0

        for para in paragraphs:
            if not para.strip():
                continue
            para_tokens = _token_count(para)

            if current_chunk_lines and (current_tokens + para_tokens > chunk_size):
                # Aktuellen Chunk abschließen
                result_sections.append((heading, "\n\n".join(current_chunk_lines)))
                current_chunk_lines = [para]
                current_tokens = ((_token_count(heading) if heading else 0) + para_tokens)
            else:
                current_chunk_lines.append(para)
                current_tokens += para_tokens

        if current_chunk_lines:
            result_sections.append((heading, "\n\n".join(current_chunk_lines)))

        return result_sections if result_sections else [(heading, content)]

    # Schritt 1: Text in Heading-Sektionen aufteilen
    sections = _split_into_sections(markdown)

    # Schritt 2: Lange Sektionen an Absatz-Grenzen weiter aufteilen
    fine_sections: list[tuple[str, str]] = []
    for heading, content in sections:
        sub_sections = _split_at_paragraphs(heading, content, chunk_size)
        fine_sections.extend(sub_sections)

    # Schritt 3: Chunks mit Metadaten erzeugen
    chunks: list[dict] = []
    for idx, (heading, content) in enumerate(fine_sections):
        # Chunk-Text zusammensetzen
        if heading and content.strip():
            chunk_text = heading + "\n\n" + content.strip()
        elif heading:
            chunk_text = heading
        else:
            chunk_text = content.strip()

        if not chunk_text:
            continue

        chunks.append({
            "index": len(chunks),
            "heading": heading,
            "source": source,
            "token_count": _token_count(chunk_text),
            "text": chunk_text,
        })

    log.debug(
        "chunk_markdown_done",
        source=source,
        chunk_count=len(chunks),
        chunk_size=chunk_size,
        total_tokens=sum(c["token_count"] for c in chunks),
    )

    return chunks


# =============================================================================
# Template Matching
# =============================================================================

def find_matching_template(document_type: str, markdown: str) -> Optional[dict]:
    """Sucht ein passendes Template für einen Dokumenttyp (T-MKIT-036).

    Schritt 1: Exakter Match per Template-ID (document_type == template.id).
    Schritt 2: Keyword-Match — alle enabled Templates mit classify_keywords werden
               gegen den Markdown-Text geprüft. Sortierung: priority DESC, matches DESC.

    Returns:
        Template-Dict (mit geparstem 'schema') oder None.
    """
    from templates_db import _return_conn
    _get_template_by_id = _get("get_template_by_id", get_template_by_id)
    _get_db_connection = _get("get_db_connection", get_db_connection)

    # Schritt 1: Exakter Match
    tmpl = _get_template_by_id(document_type)
    if tmpl is not None:
        return tmpl

    # Schritt 2: Keyword-Match
    try:
        conn = _get_db_connection()
        try:
            query = "SELECT * FROM template WHERE enabled = 1 AND classify_keywords IS NOT NULL"
            rows = None
            conn_execute = getattr(conn, "execute", None)
            if callable(conn_execute):
                try:
                    maybe_rows = conn_execute(query).fetchall()
                    if isinstance(maybe_rows, list):
                        rows = maybe_rows
                except Exception:
                    rows = None
            if rows is None:
                cur = conn.cursor()
                cur.execute(query)
                rows = cur.fetchall()
        finally:
            _return_conn(conn)
    except Exception:
        return None

    markdown_lower = markdown.lower()
    candidates: list[tuple[dict, int, int]] = []
    for row in rows:
        keywords_raw = row["classify_keywords"] or ""
        keywords = [k.strip().lower() for k in keywords_raw.split(",") if k.strip()]
        if not keywords:
            continue
        matches = sum(1 for kw in keywords if kw in markdown_lower)
        if matches > 0:
            row_dict = dict(row)
            row_dict["schema"] = json.loads(row_dict["schema"]) if isinstance(row_dict.get("schema"), str) else row_dict.get("schema", {})
            if row_dict.get("field_descriptions") and isinstance(row_dict["field_descriptions"], str):
                try:
                    row_dict["field_descriptions"] = json.loads(row_dict["field_descriptions"])
                except Exception:
                    pass
            priority = row["priority"] if row["priority"] is not None else 0
            candidates.append((row_dict, matches, priority))

    if not candidates:
        return None

    # Sortiere: priority DESC, matches DESC
    candidates.sort(key=lambda x: (x[2], x[1]), reverse=True)
    return candidates[0][0]


# =============================================================================
# Auto-Extract
# =============================================================================

async def _apply_auto_extract(
    response: "ConvertResponse",
    meta: dict,
    markdown: str,
    language: str,
    min_confidence: float,
    hints: list[str],
    request_id: Optional[str] = None,
    attempt_number: Optional[int] = None,
) -> "ConvertResponse":
    """Führt Auto-Extract nach der Konvertierung durch (T-MKIT-036).

    Klassifiziert (falls nicht bereits geschehen), sucht ein passendes Template
    und extrahiert strukturierte Daten — alles in einem Schritt.

    Mutiert meta und response in-place, gibt response zurück.
    """
    _classify = _get("classify_document", classify_document)
    _find_tmpl = _get("find_matching_template", find_matching_template)
    _extract = _get("extract_structured_data", extract_structured_data)

    def _error_response(message: str, *, details: Optional[dict[str, Any]] = None) -> "ConvertResponse":
        error_response = create_error_response(
            ErrorCode.CONVERSION_FAILED,
            message,
            meta=meta,
            details=details,
        )
        error_response.markdown = markdown
        return error_response

    # Schritt 1: Klassifizierung wenn noch nicht vorhanden
    if not meta.get("document_type"):
        classify_result = await _classify(
            markdown,
            None,
            language,
            request_id=request_id,
            attempt_number=attempt_number,
        )
        meta.update(classify_result)
        response.meta = MetaData(**{k: v for k, v in meta.items()})

    # Schritt 2: Template finden nur wenn Konfidenz ausreichend
    confidence = meta.get("document_type_confidence", 0.0) or 0.0
    doc_type = meta.get("document_type", "other") or "other"
    meta["auto_extract"] = True

    if confidence >= min_confidence and doc_type and doc_type != "other":
        tmpl = _find_tmpl(doc_type, markdown)
        if tmpl:
            meta["template_used"] = tmpl["id"]
            meta["template_version"] = tmpl.get("version", 1)
            response.meta = MetaData(**{k: v for k, v in meta.items()})
            schema = tmpl["schema"] if isinstance(tmpl["schema"], dict) else json.loads(tmpl["schema"])
            extraction = await _extract(
                markdown, schema, language,
                field_descriptions=tmpl.get("field_descriptions"),
                notes=tmpl.get("notes"),
                request_id=request_id,
                attempt_number=attempt_number,
            )
            if extraction["success"]:
                response.extracted = extraction["extracted"]
                response.meta = MetaData(**{k: v for k, v in meta.items()})
                return response
            else:
                log.warning(
                    "auto_extract_failed",
                    template=tmpl["id"],
                    doc_type=doc_type,
                    error=extraction.get("error"),
                    request_id=request_id,
                    attempt_number=attempt_number,
                )
                return _error_response(
                    f"Auto-Extract mit Template '{tmpl['id']}' fehlgeschlagen",
                    details={
                        "document_type": doc_type,
                        "document_type_confidence": confidence,
                        "template_used": tmpl["id"],
                        "template_version": tmpl.get("version", 1),
                        "extraction_error": extraction.get("error"),
                    },
                )
        else:
            meta["template_used"] = None
            meta["template_version"] = None
            response.meta = MetaData(**{k: v for k, v in meta.items()})
            hints.append(
                f"No template registered for document_type '{doc_type}'. "
                f"Register one via POST /v1/templates."
            )
            meta["hints"] = hints
            response.meta = MetaData(**{k: v for k, v in meta.items()})
            return _error_response(
                f"Auto-Extract konnte kein Template für document_type '{doc_type}' finden",
                details={
                    "document_type": doc_type,
                    "document_type_confidence": confidence,
                },
            )
    else:
        meta["template_used"] = None
        meta["template_version"] = None
        response.meta = MetaData(**{k: v for k, v in meta.items()})
        return _error_response(
            "Auto-Extract konnte keinen ausreichend sicheren Dokumenttyp bestimmen",
            details={
                "document_type": doc_type,
                "document_type_confidence": confidence,
                "min_confidence": min_confidence,
            },
        )

    return response
