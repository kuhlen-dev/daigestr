#!/usr/bin/env python3
"""
generate_all_mappings.py — Generate and apply normalize_mapping for all 143 templates.

Usage:
  python3 mcp/scripts/generate_all_mappings.py
  python3 mcp/scripts/generate_all_mappings.py --dry-run --limit 2
  python3 mcp/scripts/generate_all_mappings.py --limit 10 --delay 0.5

Options:
  --dry-run       Show what would be done, make no API calls to Mistral or daigestr
  --limit N       Process only first N templates (useful for testing)
  --delay SECS    Sleep between Mistral calls (default: 0.2s)
  --base-url URL  Daigestr REST base URL (default: http://localhost:18006)
  --skip-existing Skip templates that already have a mapping set
  --model NAME    Mistral model to use (default: mistral-large-latest)
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx


# --- Config ---
MISTRAL_API_KEY = os.environ.get("MISTRAL_API_KEY", "")
DEFAULT_BASE_URL = "http://localhost:18006"
DEFAULT_MISTRAL_MODEL = "mistral-large-latest"
DEFAULT_DELAY = 0.2

def _load_normalized_fields(base_url: str) -> list[str]:
    """Load normalized field names from daigestr API."""
    resp = httpx.get(f"{base_url}/v1/normalized/fields", timeout=10.0)
    resp.raise_for_status()
    return [f["name"] for f in resp.json().get("fields", [])]


def _extract_schema_paths(schema: dict, prefix: str = "") -> list[str]:
    """Recursively extract dot-notation paths from a JSON schema."""
    paths = []
    props = schema.get("properties", {})
    for key, val in props.items():
        full_key = f"{prefix}.{key}" if prefix else key
        paths.append(full_key)
        if isinstance(val, dict) and val.get("type") == "object":
            paths.extend(_extract_schema_paths(val, full_key))
        elif isinstance(val, dict) and val.get("type") == "array":
            items = val.get("items", {})
            if isinstance(items, dict) and items.get("type") == "object":
                paths.extend(_extract_schema_paths(items, full_key + "[]"))
    return paths


def _build_mapping_prompt(template_name: str, schema_paths: list[str]) -> str:
    paths_str = "\n".join(f"  - {p}" for p in schema_paths[:200])
    fields_str = "\n".join(f"  - {f}" for f in NORMALIZED_FIELDS)
    return f"""You are a schema mapping expert. Match normalized target fields to source paths.

Template: {template_name}

Available source paths (dot-notation):
{paths_str if schema_paths else "  (no paths — simple flat schema)"}

Normalized target fields:
{fields_str}

For each target field, find the best source path match using semantic similarity.
Common mappings:
- amount: total, gesamtbetrag, betrag, brutto
- amount_net: netto, nettobetrag, net_amount
- amount_tax: steuer, mwst, tax
- invoice_number: rechnungsnummer, belegnummer, nummer
- vendor_name: lieferant.name, aussteller.name, anbieter.name, firma, name
- recipient_name: empfaenger.name, kunde.name, kaeufer.name
- date_issued: datum, rechnungsdatum, ausstellungsdatum, date
- date_due: faelligkeitsdatum, zahlungsziel, due_date
- currency: waehrung, currency
- iban_vendor: iban, bankverbindung.iban
- tax_rate: steuersatz, mwst_satz

Return ONLY a JSON object. Keys = target field names. Values = matching source path or null.
Only use paths from the available list above."""


def fetch_templates(base_url: str) -> dict:
    """Fetch all templates from /v1/templates."""
    response = httpx.get(f"{base_url}/v1/templates", timeout=30.0)
    response.raise_for_status()
    data = response.json()
    return data.get("templates", {})


def fetch_existing_mappings(base_url: str, template_name: str) -> bool:
    """Return True if template already has a mapping."""
    try:
        response = httpx.get(
            f"{base_url}/v1/normalized/mappings/{template_name}",
            timeout=10.0,
        )
        if response.status_code == 200:
            data = response.json()
            mapping = data.get("normalize_mapping", {})
            return bool(mapping)
    except Exception:
        pass
    return False


def generate_mapping_via_mistral(
    template_name: str,
    schema: dict,
    model: str,
    api_key: str,
) -> dict:
    """Call Mistral API to generate a mapping for a template schema."""
    schema_paths = _extract_schema_paths(schema)
    prompt = _build_mapping_prompt(template_name, schema_paths)

    response = httpx.post(
        "https://api.mistral.ai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        },
        timeout=60.0,
    )
    response.raise_for_status()

    content = response.json()["choices"][0]["message"]["content"]
    raw_mapping = json.loads(content)

    # Normalize: keep only known fields, ensure all fields present
    mapping = {}
    for field in NORMALIZED_FIELDS:
        val = raw_mapping.get(field)
        mapping[field] = val if isinstance(val, str) and val.strip() else None

    return mapping


def _dry_run_mapping(template_name: str, schema: dict) -> dict:
    """Generate a simple heuristic mapping without API calls (for --dry-run)."""
    schema_paths = _extract_schema_paths(schema)
    # Simple keyword-based heuristic for dry-run preview
    heuristics = {
        "amount": ["gesamtbetrag", "total", "betrag", "brutto"],
        "amount_net": ["nettobetrag", "netto", "net_amount"],
        "amount_tax": ["steuerbetrag", "mwst", "steuer", "tax"],
        "invoice_number": ["rechnungsnummer", "belegnummer", "nummer"],
        "date_issued": ["datum", "rechnungsdatum", "ausstellungsdatum"],
        "date_due": ["faelligkeitsdatum", "zahlungsziel"],
        "vendor_name": ["lieferant.name", "aussteller.name", "firma", "name"],
        "currency": ["waehrung", "currency"],
        "tax_rate": ["steuersatz", "mwst_satz"],
    }
    mapping = {f: None for f in NORMALIZED_FIELDS}
    for norm_field, keywords in heuristics.items():
        for kw in keywords:
            for path in schema_paths:
                if kw in path.lower():
                    mapping[norm_field] = path
                    break
            if mapping[norm_field]:
                break
    return mapping


def apply_mapping(base_url: str, template_name: str, mapping: dict) -> bool:
    """PUT the mapping to /v1/normalized/mappings/{template_name}."""
    clean_mapping = {k: v for k, v in mapping.items() if v is not None}
    response = httpx.put(
        f"{base_url}/v1/normalized/mappings/{template_name}",
        json={"normalize_mapping": clean_mapping},
        timeout=30.0,
    )
    return response.status_code in (200, 201, 204)


def print_coverage_report(base_url: str) -> None:
    """Fetch and print the coverage report."""
    try:
        # List all templates and count those with mappings
        response = httpx.get(f"{base_url}/v1/templates", timeout=30.0)
        templates = response.json().get("templates", {})
        total = len(templates)

        mapped = 0
        for tname in templates:
            try:
                r = httpx.get(
                    f"{base_url}/v1/normalized/mappings/{tname}",
                    timeout=5.0,
                )
                if r.status_code == 200:
                    data = r.json()
                    if data.get("normalize_mapping"):
                        mapped += 1
            except Exception:
                pass

        pct = (mapped / total * 100) if total else 0
        print(f"\n{'='*60}")
        print(f"Coverage Report")
        print(f"{'='*60}")
        print(f"Total templates : {total}")
        print(f"With mappings   : {mapped}")
        print(f"Coverage        : {pct:.1f}%")
        bar_len = 40
        filled = int(bar_len * pct / 100)
        bar = "█" * filled + "░" * (bar_len - filled)
        print(f"Progress        : [{bar}] {pct:.1f}%")
    except Exception as exc:
        print(f"Coverage report failed: {exc}")


def main():
    parser = argparse.ArgumentParser(
        description="Generate normalize_mapping for all daigestr templates via Mistral."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview only, no API calls to Mistral or daigestr")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process only first N templates")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY,
                        help=f"Delay between Mistral calls in seconds (default: {DEFAULT_DELAY})")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL,
                        help=f"Daigestr REST base URL (default: {DEFAULT_BASE_URL})")
    parser.add_argument("--skip-existing", action="store_true",
                        help="Skip templates that already have a mapping")
    parser.add_argument("--model", default=DEFAULT_MISTRAL_MODEL,
                        help=f"Mistral model (default: {DEFAULT_MISTRAL_MODEL})")
    args = parser.parse_args()

    # Load normalized fields from API
    global NORMALIZED_FIELDS
    try:
        NORMALIZED_FIELDS = _load_normalized_fields(args.base_url)
    except Exception as exc:
        print(f"ERROR: Could not load normalized fields: {exc}", file=sys.stderr)
        sys.exit(1)

    # Resolve API key
    api_key = MISTRAL_API_KEY
    if not api_key and not args.dry_run:
        # Try loading from .env
        env_path = Path(__file__).parent.parent.parent / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("MISTRAL_API_KEY="):
                    api_key = line.split("=", 1)[1].strip()
                    break
    if not api_key and not args.dry_run:
        print("ERROR: MISTRAL_API_KEY not set. Use env var or .env file.", file=sys.stderr)
        sys.exit(1)

    print(f"Fetching templates from {args.base_url}...")
    try:
        templates = fetch_templates(args.base_url)
    except Exception as exc:
        print(f"ERROR: Could not fetch templates: {exc}", file=sys.stderr)
        sys.exit(1)

    template_names = list(templates.keys())
    if args.limit:
        template_names = template_names[: args.limit]

    total = len(template_names)
    print(f"Processing {total} templates (dry_run={args.dry_run}, skip_existing={args.skip_existing})")
    print(f"Normalized fields: {len(NORMALIZED_FIELDS)}")
    print()

    ok_count = 0
    skip_count = 0
    error_count = 0
    errors = []

    for idx, template_name in enumerate(template_names, 1):
        schema = templates[template_name]
        schema_paths = _extract_schema_paths(schema)
        path_count = len(schema_paths)

        prefix = f"Template {idx}/{total}: {template_name}"

        # Skip existing?
        if args.skip_existing and not args.dry_run:
            if fetch_existing_mappings(args.base_url, template_name):
                print(f"  {prefix} → SKIPPED (already mapped)")
                skip_count += 1
                continue

        if args.dry_run:
            mapping = _dry_run_mapping(template_name, schema)
            mapped_count = sum(1 for v in mapping.values() if v is not None)
            print(f"  {prefix} → DRY-RUN: {mapped_count}/{len(NORMALIZED_FIELDS)} fields mapped "
                  f"({path_count} schema paths)")
            ok_count += 1
            continue

        # Generate via Mistral
        try:
            mapping = generate_mapping_via_mistral(
                template_name=template_name,
                schema=schema,
                model=args.model,
                api_key=api_key,
            )
        except httpx.HTTPStatusError as exc:
            msg = f"Mistral API error: {exc.response.status_code}"
            print(f"  {prefix} → ERROR: {msg}")
            errors.append({"template": template_name, "error": msg})
            error_count += 1
            continue
        except Exception as exc:
            msg = str(exc)
            print(f"  {prefix} → ERROR: {msg}")
            errors.append({"template": template_name, "error": msg})
            error_count += 1
            continue

        mapped_count = sum(1 for v in mapping.values() if v is not None)

        # Apply to daigestr
        try:
            success = apply_mapping(args.base_url, template_name, mapping)
        except Exception as exc:
            msg = f"Apply failed: {exc}"
            print(f"  {prefix} → ERROR: {msg}")
            errors.append({"template": template_name, "error": msg})
            error_count += 1
            continue

        if success:
            print(f"  {prefix} → {mapped_count} fields mapped")
            ok_count += 1
        else:
            msg = "PUT returned non-2xx"
            print(f"  {prefix} → ERROR: {msg}")
            errors.append({"template": template_name, "error": msg})
            error_count += 1

        if args.delay > 0 and idx < total:
            time.sleep(args.delay)

    # Summary
    print()
    print(f"{'='*60}")
    print(f"Summary")
    print(f"{'='*60}")
    print(f"Processed : {total}")
    print(f"OK        : {ok_count}")
    print(f"Skipped   : {skip_count}")
    print(f"Errors    : {error_count}")

    if errors:
        print(f"\nErrors:")
        for e in errors:
            print(f"  - {e['template']}: {e['error']}")

    if not args.dry_run:
        print_coverage_report(args.base_url)


if __name__ == "__main__":
    main()
