"""
REST API Admin-Endpoints für Normalization Management (T-DAI-056).

Verwaltet normalized_fields, normalized_values, normalized_categories,
normalize_mapping pro Template und extraction_corrections.

Alle Endpoints unter /v1/normalized/* und /v1/corrections/*.
"""

from typing import Any, Optional

import structlog
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from normalizer_db import (
    get_categories,
    create_category,
    update_category,
    get_fields,
    get_field,
    create_field,
    update_field,
    get_values,
    create_value,
    update_value,
    get_mapping,
    set_mapping,
    get_corrections,
    create_correction,
    review_correction,
)
from normalizer_cache import cache_reset

log = structlog.get_logger()

normalize_router = APIRouter(prefix="/v1/normalized", tags=["normalization"])
corrections_router = APIRouter(prefix="/v1/corrections", tags=["normalization"])
batch_router = APIRouter(prefix="/v1/normalize", tags=["normalization"])


# =============================================================================
# Pydantic Request Models
# =============================================================================

class FieldCreateRequest(BaseModel):
    name: str = Field(..., description="Eindeutiger Feldname (snake_case)")
    label_de: str = Field(..., description="Deutsches Label")
    label_en: str = Field(..., description="Englisches Label")
    type: str = Field(..., description="Feldtyp: string, decimal, date, boolean, enum")
    category: str = Field(..., description="Kategorie-Name aus normalized_categories")
    description: str = Field(..., description="Beschreibung des Feldes")
    validation_rules: Optional[dict[str, Any]] = Field(None, description="Validierungsregeln (min, max, pattern, mod97, iso4217, iso8601)")
    default_value: Optional[str] = Field(None, description="Standardwert als String")
    default_context: Optional[dict[str, Any]] = Field(None, description="Kontext-abhängige Standardwerte")
    is_array: bool = Field(False, description="Ist das Feld ein Array?")
    sort_order: int = Field(100, description="Sortierreihenfolge")


class FieldUpdateRequest(BaseModel):
    label_de: Optional[str] = Field(None, description="Deutsches Label")
    label_en: Optional[str] = Field(None, description="Englisches Label")
    type: Optional[str] = Field(None, description="Feldtyp")
    category: Optional[str] = Field(None, description="Kategorie-Name")
    description: Optional[str] = Field(None, description="Beschreibung")
    validation_rules: Optional[dict[str, Any]] = Field(None, description="Validierungsregeln")
    default_value: Optional[str] = Field(None, description="Standardwert")
    default_context: Optional[dict[str, Any]] = Field(None, description="Kontext-Standardwerte")
    is_array: Optional[bool] = Field(None, description="Array-Flag")
    sort_order: Optional[int] = Field(None, description="Sortierreihenfolge")
    active: Optional[bool] = Field(None, description="Aktiv/Inaktiv")


class ValueCreateRequest(BaseModel):
    field_name: str = Field(..., description="Feldname aus normalized_fields")
    canonical_value: str = Field(..., description="Kanonischer Wert (Zielwert)")
    aliases: Optional[list[str]] = Field(None, description="Alias-Werte (alternative Schreibweisen)")
    context: Optional[dict[str, Any]] = Field(None, description="Kontext-Metadaten")
    is_default: bool = Field(False, description="Ist dies der Standardwert für das Feld?")
    source: str = Field("user", description="Quelle: system, managed, user")
    description: Optional[str] = Field(None, description="Beschreibung des Wertes")
    sort_order: int = Field(100, description="Sortierreihenfolge")


class ValueUpdateRequest(BaseModel):
    canonical_value: Optional[str] = Field(None, description="Kanonischer Wert")
    aliases: Optional[list[str]] = Field(None, description="Alias-Werte")
    context: Optional[dict[str, Any]] = Field(None, description="Kontext-Metadaten")
    is_default: Optional[bool] = Field(None, description="Standardwert-Flag")
    source: Optional[str] = Field(None, description="Quelle")
    description: Optional[str] = Field(None, description="Beschreibung")
    sort_order: Optional[int] = Field(None, description="Sortierreihenfolge")
    active: Optional[bool] = Field(None, description="Aktiv/Inaktiv")


class MappingSetRequest(BaseModel):
    normalize_mapping: dict[str, Any] = Field(
        ...,
        description="Mapping normalisierter Feldnamen → Dot-Notation-Pfade im extracted-Dict. "
                    "Beispiel: {'amount': 'total'} oder Fallback-Ketten: {'vendor': ['_meta.firma', 'vendor']}"
    )
    required_normalized_fields: Optional[list[str]] = Field(
        None, description="Pflichtfelder — fehlende Felder erzeugen Warnungen."
    )


class CorrectionCreateRequest(BaseModel):
    field_name: str = Field(..., description="Feldname aus normalized_fields")
    new_value: str = Field(..., description="Korrigierter Wert")
    document_id: Optional[str] = Field(None, description="Dokument-ID (z.B. Job-ID oder Dateipfad)")
    template_name: Optional[str] = Field(None, description="Template-Name")
    old_value: Optional[str] = Field(None, description="Ursprünglicher (falscher) Wert")
    source: str = Field("user", description="Korrektur-Quelle: user, system")


class CorrectionReviewRequest(BaseModel):
    reviewed_by: str = Field(..., description="Reviewer (User-ID oder Name)")
    applied: bool = Field(True, description="Wurde die Korrektur angewendet?")


class BatchNormalizeRequest(BaseModel):
    template_name: str = Field(..., description="Template-Name für Normalisierung")
    records: list[dict[str, Any]] = Field(
        ...,
        description="Liste von extracted-Dicts zur Batch-Normalisierung",
        min_length=1,
        max_length=500,
    )
    compact: bool = Field(False, description="Null-Felder aus Output entfernen")
    meta: dict[str, Any] = Field(default_factory=dict, description="Gemeinsame Metadaten für alle Records")


# =============================================================================
# Fields
# =============================================================================

@normalize_router.get("/fields", summary="Alle normalisierten Felder auflisten")
def list_fields(
    category: Optional[str] = Query(None, description="Nach Kategorie filtern"),
    active_only: bool = Query(True, description="Nur aktive Felder zurückgeben"),
) -> dict[str, Any]:
    fields = get_fields(category=category, active_only=active_only)
    return {"fields": fields, "count": len(fields)}


@normalize_router.post(
    "/fields",
    summary="Neues normalisiertes Feld erstellen",
    description="Erstellt ein neues Feld in normalized_fields. Nach dem Erstellen wird der Cache zurückgesetzt.",
    status_code=201,
)
def create_field_endpoint(body: FieldCreateRequest) -> dict[str, Any]:
    try:
        field = create_field(
            name=body.name,
            label_de=body.label_de,
            label_en=body.label_en,
            type=body.type,
            category=body.category,
            description=body.description,
            validation_rules=body.validation_rules,
            default_value=body.default_value,
            default_context=body.default_context,
            is_array=body.is_array,
            sort_order=body.sort_order,
        )
        cache_reset()
        log.info("normalized_field_created", name=body.name)
        return {"field": field}
    except Exception as exc:
        log.error("normalized_field_create_error", name=body.name, error=str(exc))
        raise HTTPException(status_code=400, detail=str(exc))


@normalize_router.put(
    "/fields/{name}",
    summary="Normalisiertes Feld aktualisieren",
    description="Aktualisiert ein bestehendes Feld. Nur übergebene Felder werden geändert (partial update).",
)
def update_field_endpoint(name: str, body: FieldUpdateRequest) -> dict[str, Any]:
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="Keine Felder zum Aktualisieren angegeben")
    updated = update_field(name, **updates)
    if updated is None:
        raise HTTPException(status_code=404, detail=f"Feld '{name}' nicht gefunden")
    cache_reset()
    return {"field": updated}


# =============================================================================
# Values
# =============================================================================

@normalize_router.get("/values", summary="Kanonische Werte auflisten")
def list_values(
    field_name: str = Query(..., description="Feldname aus normalized_fields"),
    active_only: bool = Query(True, description="Nur aktive Werte zurückgeben"),
) -> dict[str, Any]:
    values = get_values(field_name=field_name, active_only=active_only)
    return {"values": values, "field_name": field_name, "count": len(values)}


@normalize_router.post(
    "/values",
    summary="Neuen kanonischen Wert erstellen",
    description="Fügt einen neuen Wert zur normalized_values-Tabelle hinzu. Aliases werden für Fuzzy-Matching genutzt.",
    status_code=201,
)
def create_value_endpoint(body: ValueCreateRequest) -> dict[str, Any]:
    try:
        value = create_value(
            field_name=body.field_name,
            canonical_value=body.canonical_value,
            aliases=body.aliases,
            context=body.context,
            is_default=body.is_default,
            source=body.source,
            description=body.description,
            sort_order=body.sort_order,
        )
        cache_reset()
        log.info("normalized_value_created", field=body.field_name, value=body.canonical_value)
        return {"value": value}
    except Exception as exc:
        log.error("normalized_value_create_error", field=body.field_name, error=str(exc))
        raise HTTPException(status_code=400, detail=str(exc))


@normalize_router.put(
    "/values/{value_id}",
    summary="Kanonischen Wert aktualisieren",
    description="Aktualisiert einen Wert per ID. Partial update — nur übergebene Felder werden geändert.",
)
def update_value_endpoint(value_id: int, body: ValueUpdateRequest) -> dict[str, Any]:
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="Keine Felder zum Aktualisieren angegeben")
    updated = update_value(value_id, **updates)
    if updated is None:
        raise HTTPException(status_code=404, detail=f"Wert mit ID {value_id} nicht gefunden")
    cache_reset()
    return {"value": updated}


# =============================================================================
# Categories
# =============================================================================

@normalize_router.get("/categories", summary="Alle Normalisierungs-Kategorien auflisten")
def list_categories(
    active_only: bool = Query(True, description="Nur aktive Kategorien zurückgeben"),
) -> dict[str, Any]:
    categories = get_categories(active_only=active_only)
    return {"categories": categories, "count": len(categories)}


# =============================================================================
# Template Mappings
# =============================================================================

@normalize_router.get(
    "/mappings/{template_name}",
    summary="Normalisierungs-Mapping eines Templates abrufen",
    description="Gibt das normalize_mapping und required_normalized_fields für ein Template zurück.",
)
def get_template_mapping(template_name: str) -> dict[str, Any]:
    mapping = get_mapping(template_name)
    if mapping is None:
        raise HTTPException(
            status_code=404,
            detail=f"Kein Mapping für Template '{template_name}' gefunden"
        )
    return {"template_name": template_name, **mapping}


@normalize_router.put(
    "/mappings/{template_name}",
    summary="Normalisierungs-Mapping eines Templates setzen",
    description="Setzt oder überschreibt das normalize_mapping für ein Template. Dot-Notation-Pfade aus extracted → normalisierte Felder. Cache wird zurückgesetzt.",
)
def set_template_mapping(template_name: str, body: MappingSetRequest) -> dict[str, Any]:
    ok = set_mapping(
        template_name=template_name,
        mapping=body.normalize_mapping,
        required_fields=body.required_normalized_fields,
    )
    if not ok:
        raise HTTPException(
            status_code=404,
            detail=f"Template '{template_name}' nicht gefunden — zuerst via POST /v1/templates erstellen"
        )
    cache_reset()
    log.info("normalize_mapping_set", template=template_name)
    return {
        "template_name": template_name,
        "normalize_mapping": body.normalize_mapping,
        "required_normalized_fields": body.required_normalized_fields,
        "message": "Mapping gesetzt, Cache zurückgesetzt",
    }


# =============================================================================
# Schema (JSON Schema aus normalized_fields)
# =============================================================================

@normalize_router.get(
    "/schema",
    summary="JSON Schema aus normalisierten Feldern generieren",
    description="Generiert ein JSON Schema aus allen aktiven normalized_fields. Nützlich für Validierung und Dokumentation.",
)
def get_normalized_schema(
    category: Optional[str] = Query(None, description="Schema auf Kategorie einschränken"),
) -> dict[str, Any]:
    fields = get_fields(category=category, active_only=True)
    type_map = {
        "string": "string",
        "decimal": "number",
        "date": "string",
        "boolean": "boolean",
        "enum": "string",
    }
    properties: dict[str, Any] = {}
    for f in fields:
        prop: dict[str, Any] = {
            "type": type_map.get(f.get("type", "string"), "string"),
            "description": f.get("description", ""),
        }
        if f.get("type") == "date":
            prop["format"] = "date"
        if f.get("type") == "decimal":
            prop["type"] = "number"
        if f.get("is_array"):
            prop = {"type": "array", "items": prop}
        if f.get("validation_rules"):
            rules = f["validation_rules"]
            if "min" in rules:
                prop["minimum"] = rules["min"]
            if "max" in rules:
                prop["maximum"] = rules["max"]
            if "pattern" in rules:
                prop["pattern"] = rules["pattern"]
        properties[f["name"]] = prop

    return {
        "type": "object",
        "title": "NormalizedOutput",
        "description": "Normalisierter Output nach Daigestr-Normalisierung",
        "properties": properties,
        "field_count": len(fields),
    }


# =============================================================================
# Coverage Report
# =============================================================================

@normalize_router.get(
    "/coverage",
    summary="Coverage-Report: Welche Templates haben ein Normalisierungs-Mapping?",
    description="Zeigt für jedes Template ob ein normalize_mapping vorhanden ist. Hilfreich um Lücken zu finden.",
)
def get_coverage() -> dict[str, Any]:
    from templates_db import get_db_connection, _return_conn
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, display_name, category, "
            "normalize_mapping IS NOT NULL AS has_mapping, "
            "array_length(required_normalized_fields, 1) AS required_count "
            "FROM template ORDER BY category, id"
        )
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        _return_conn(conn)

    with_mapping = [r for r in rows if r["has_mapping"]]
    without_mapping = [r for r in rows if not r["has_mapping"]]

    return {
        "total_templates": len(rows),
        "with_mapping": len(with_mapping),
        "without_mapping": len(without_mapping),
        "coverage_pct": round(len(with_mapping) / len(rows) * 100, 1) if rows else 0.0,
        "templates_with_mapping": with_mapping,
        "templates_without_mapping": without_mapping,
    }


# =============================================================================
# Batch Re-Normalisierung
# =============================================================================

@batch_router.post(
    "/batch",
    summary="Batch Re-Normalisierung",
    description="Normalisiert eine Liste von extracted-Dicts mit einem Template. Max 500 Records pro Aufruf.",
)
async def batch_normalize(body: BatchNormalizeRequest) -> dict[str, Any]:
    from normalizer import normalize
    results = []
    errors = []
    for i, record in enumerate(body.records):
        try:
            result = await normalize(
                extracted=record,
                template_name=body.template_name,
                meta=body.meta,
                compact=body.compact,
            )
            results.append({
                "index": i,
                "success": result is not None,
                "result": result,
            })
        except Exception as exc:
            errors.append({"index": i, "error": str(exc)})
            results.append({"index": i, "success": False, "result": None})

    return {
        "template_name": body.template_name,
        "total": len(body.records),
        "succeeded": sum(1 for r in results if r["success"]),
        "failed": len(errors),
        "results": results,
        "errors": errors,
    }


# =============================================================================
# Corrections
# =============================================================================

@corrections_router.get(
    "",
    summary="Alle Korrekturen auflisten",
    description="Gibt Korrektureinträge aus extraction_corrections zurück, optional gefiltert.",
)
def list_corrections(
    template_name: Optional[str] = Query(None, description="Nach Template filtern"),
    field_name: Optional[str] = Query(None, description="Nach Feld filtern"),
    applied: Optional[bool] = Query(None, description="Nach angewendet/nicht-angewendet filtern"),
) -> dict[str, Any]:
    corrections = get_corrections(
        template_name=template_name,
        field_name=field_name,
        applied=applied,
    )
    return {"corrections": corrections, "count": len(corrections)}


@corrections_router.post(
    "",
    summary="Neue Korrektur erfassen",
    description="Erfasst eine manuelle Korrektur für einen extrahierten Feldwert (Qualitätssicherung, Re-Training).",
    status_code=201,
)
def create_correction_endpoint(body: CorrectionCreateRequest) -> dict[str, Any]:
    try:
        correction = create_correction(
            field_name=body.field_name,
            new_value=body.new_value,
            document_id=body.document_id,
            template_name=body.template_name,
            old_value=body.old_value,
            source=body.source,
        )
        log.info("correction_created", field=body.field_name, template=body.template_name)
        return {"correction": correction}
    except Exception as exc:
        log.error("correction_create_error", field=body.field_name, error=str(exc))
        raise HTTPException(status_code=400, detail=str(exc))


@corrections_router.put(
    "/{correction_id}",
    summary="Korrektur reviewen",
    description="Markiert eine Korrektur als reviewt und optional als angewendet.",
)
def review_correction_endpoint(correction_id: int, body: CorrectionReviewRequest) -> dict[str, Any]:
    updated = review_correction(
        correction_id=correction_id,
        reviewed_by=body.reviewed_by,
        applied=body.applied,
    )
    if updated is None:
        raise HTTPException(
            status_code=404,
            detail=f"Korrektur mit ID {correction_id} nicht gefunden"
        )
    return {"correction": updated}
