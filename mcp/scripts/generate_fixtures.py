#!/usr/bin/env python3
"""
generate_fixtures.py — Generate test fixtures for templates that have mappings.

For each template with a normalize_mapping, generates a synthetic extracted dict
and expected normalized dict via Mistral API, then stores it via POST
/v1/normalized/fixtures/{template_name}.

Usage:
  python3 mcp/scripts/generate_fixtures.py
  python3 mcp/scripts/generate_fixtures.py --dry-run --limit 3
  python3 mcp/scripts/generate_fixtures.py --limit 10 --delay 0.5

Options:
  --dry-run       Show what would be generated, make no API or storage calls
  --limit N       Process only first N templates
  --delay SECS    Sleep between Mistral calls (default: 0.5s)
  --base-url URL  Daigestr REST base URL (default: http://localhost:18006)
  --model NAME    Mistral model (default: mistral-large-latest)
  --skip-existing Skip templates that already have fixtures
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx


MISTRAL_API_KEY = os.environ.get("MISTRAL_API_KEY", "")
DEFAULT_BASE_URL = "http://localhost:18006"
DEFAULT_MISTRAL_MODEL = "mistral-large-latest"
DEFAULT_DELAY = 0.5

# All 52 normalized fields
NORMALIZED_FIELDS = [
    "amount", "amount_net", "amount_tax", "tax_rate", "currency",
    "iban_vendor", "iban_recipient", "bic", "payment_method", "mandate_reference",
    "tax_relevant", "tax_category", "tax_deductible_amount",
    "invoice_number", "reference_number", "order_number", "customer_number",
    "contract_number", "insurance_number",
    "vendor_name", "vendor_address", "vendor_contact", "vendor_tax_id",
    "vendor_slug", "vendor_country",
    "recipient_name", "recipient_address", "recipient_country",
    "date_issued", "date_due", "date_paid", "date_service", "treatment_date",
    "date_period_from", "date_period_to",
    "line_items", "line_items_count", "summary", "notes",
    "cancellation_period", "contract_end", "auto_renewal",
    "phone_number", "insurance_type", "reimbursement_rate", "own_share", "diagnosis",
    "language", "page_count", "quality_score", "completeness_score", "tax_country",
]


def fetch_templates(base_url: str) -> dict:
    response = httpx.get(f"{base_url}/v1/templates", timeout=30.0)
    response.raise_for_status()
    return response.json().get("templates", {})


def fetch_mapping(base_url: str, template_name: str) -> dict | None:
    """Fetch the normalize_mapping for a template. Returns None if not set."""
    try:
        response = httpx.get(
            f"{base_url}/v1/normalized/mappings/{template_name}",
            timeout=10.0,
        )
        if response.status_code == 200:
            data = response.json()
            mapping = data.get("normalize_mapping", {})
            return mapping if mapping else None
    except Exception:
        pass
    return None


def has_existing_fixtures(base_url: str, template_name: str) -> bool:
    """Check if fixtures already exist for a template."""
    try:
        response = httpx.get(
            f"{base_url}/v1/normalized/fixtures/{template_name}",
            timeout=10.0,
        )
        if response.status_code == 200:
            data = response.json()
            fixtures = data.get("fixtures", [])
            return len(fixtures) > 0
    except Exception:
        pass
    return False


def _build_fixture_prompt(template_name: str, mapping: dict, schema: dict) -> str:
    # Only include fields that are actually mapped (non-null values)
    active_mapping = {k: v for k, v in mapping.items() if v}

    mapping_str = "\n".join(f"  {k} <- {v}" for k, v in active_mapping.items())
    schema_title = schema.get("title", template_name)
    schema_desc = schema.get("description", "")

    return f"""Generate a synthetic test fixture for a document extraction template.

Template: {template_name}
Description: {schema_title} — {schema_desc}

Active field mappings (normalized_field <- source_path):
{mapping_str if mapping_str else "  (no active mappings)"}

Generate two JSON objects:

1. "input_extracted": A realistic synthetic extracted document. Use the source paths as keys.
   - Use realistic but clearly fictional values (no real PII)
   - For amounts: use decimal numbers like 119.00, 100.00, 19.00
   - For dates: use ISO format like "2024-03-15"
   - For names: use clearly fictional names like "Musterfirma GmbH", "Max Mustermann"
   - For addresses: use "Musterstraße 1, 12345 Musterstadt"
   - For IBANs: use "DE89370400440532013000" (test IBAN)
   - For currencies: use "EUR"

2. "expected_normalized": The expected normalized output after applying the mapping.
   - Keys: normalized field names from the mapping
   - Values: what the normalizer should output (after type conversion if applicable)
   - Dates should be in ISO format YYYY-MM-DD
   - Amounts as numbers (float)

Return ONLY a JSON object with exactly these two keys: "input_extracted" and "expected_normalized"."""


def generate_fixture_via_mistral(
    template_name: str,
    mapping: dict,
    schema: dict,
    model: str,
    api_key: str,
) -> dict:
    """Call Mistral API to generate a test fixture."""
    prompt = _build_fixture_prompt(template_name, mapping, schema)

    response = httpx.post(
        "https://api.mistral.ai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "response_format": {"type": "json_object"},
        },
        timeout=60.0,
    )
    response.raise_for_status()

    content = response.json()["choices"][0]["message"]["content"]
    data = json.loads(content)

    input_extracted = data.get("input_extracted", {})
    expected_normalized = data.get("expected_normalized", {})

    if not isinstance(input_extracted, dict):
        input_extracted = {}
    if not isinstance(expected_normalized, dict):
        expected_normalized = {}

    return {
        "input_extracted": input_extracted,
        "expected_normalized": expected_normalized,
    }


def _dry_run_fixture(template_name: str, mapping: dict) -> dict:
    """Generate a minimal heuristic fixture for --dry-run."""
    active = {k: v for k, v in mapping.items() if v}
    input_extracted = {}
    expected_normalized = {}

    for norm_field, source_path in active.items():
        # Set a simple placeholder in extracted using the source path as flat key
        leaf = source_path.split(".")[-1] if "." in source_path else source_path
        if norm_field in ("amount", "amount_net", "amount_tax", "tax_rate",
                          "tax_deductible_amount", "reimbursement_rate", "own_share"):
            input_extracted[leaf] = "119,00"
            expected_normalized[norm_field] = 119.0
        elif norm_field.startswith("date_") or norm_field in ("treatment_date",):
            input_extracted[leaf] = "15.03.2024"
            expected_normalized[norm_field] = "2024-03-15"
        elif norm_field == "currency":
            input_extracted[leaf] = "EUR"
            expected_normalized[norm_field] = "EUR"
        else:
            input_extracted[leaf] = f"Muster-{norm_field}"
            expected_normalized[norm_field] = f"Muster-{norm_field}"

    return {
        "input_extracted": input_extracted,
        "expected_normalized": expected_normalized,
    }


def store_fixture(
    base_url: str,
    template_name: str,
    input_extracted: dict,
    expected_normalized: dict,
    description: str = "",
) -> bool:
    """POST fixture to /v1/normalized/fixtures/{template_name}."""
    response = httpx.post(
        f"{base_url}/v1/normalized/fixtures/{template_name}",
        json={
            "input_extracted": input_extracted,
            "expected_normalized": expected_normalized,
            "description": description,
        },
        timeout=30.0,
    )
    return response.status_code in (200, 201)


def main():
    parser = argparse.ArgumentParser(
        description="Generate test fixtures for all mapped daigestr templates."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview only, no Mistral or storage calls")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process only first N templates")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY,
                        help=f"Delay between calls in seconds (default: {DEFAULT_DELAY})")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL,
                        help=f"Daigestr REST base URL (default: {DEFAULT_BASE_URL})")
    parser.add_argument("--model", default=DEFAULT_MISTRAL_MODEL,
                        help=f"Mistral model (default: {DEFAULT_MISTRAL_MODEL})")
    parser.add_argument("--skip-existing", action="store_true",
                        help="Skip templates that already have fixtures")
    args = parser.parse_args()

    # Resolve API key
    api_key = MISTRAL_API_KEY
    if not api_key and not args.dry_run:
        env_path = Path(__file__).parent.parent.parent / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("MISTRAL_API_KEY="):
                    api_key = line.split("=", 1)[1].strip()
                    break
    if not api_key and not args.dry_run:
        print("ERROR: MISTRAL_API_KEY not set.", file=sys.stderr)
        sys.exit(1)

    print(f"Fetching templates from {args.base_url}...")
    try:
        templates = fetch_templates(args.base_url)
    except Exception as exc:
        print(f"ERROR: Could not fetch templates: {exc}", file=sys.stderr)
        sys.exit(1)

    # Collect only templates with mappings
    templates_with_mappings = []
    print("Checking which templates have mappings...")
    for name in templates:
        if args.dry_run:
            # In dry-run: pretend all have mappings (use empty mapping)
            templates_with_mappings.append((name, {}, templates[name]))
        else:
            mapping = fetch_mapping(args.base_url, name)
            if mapping:
                templates_with_mappings.append((name, mapping, templates[name]))

    if args.limit:
        templates_with_mappings = templates_with_mappings[: args.limit]

    total = len(templates_with_mappings)
    print(f"Templates with mappings: {total} (dry_run={args.dry_run})")
    print()

    ok_count = 0
    skip_count = 0
    error_count = 0
    errors = []

    for idx, (template_name, mapping, schema) in enumerate(templates_with_mappings, 1):
        prefix = f"Fixture {idx}/{total}: {template_name}"

        if args.skip_existing and not args.dry_run:
            if has_existing_fixtures(args.base_url, template_name):
                print(f"  {prefix} → SKIPPED (already has fixtures)")
                skip_count += 1
                continue

        if args.dry_run:
            fixture = _dry_run_fixture(template_name, mapping)
            in_count = len(fixture["input_extracted"])
            exp_count = len(fixture["expected_normalized"])
            print(f"  {prefix} → DRY-RUN: input={in_count} fields, expected={exp_count} fields")
            ok_count += 1
            continue

        # Generate via Mistral
        try:
            fixture = generate_fixture_via_mistral(
                template_name=template_name,
                mapping=mapping,
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
            print(f"  {prefix} → ERROR generating: {msg}")
            errors.append({"template": template_name, "error": msg})
            error_count += 1
            continue

        # Store fixture
        try:
            success = store_fixture(
                base_url=args.base_url,
                template_name=template_name,
                input_extracted=fixture["input_extracted"],
                expected_normalized=fixture["expected_normalized"],
                description=f"Auto-generated via generate_fixtures.py ({args.model})",
            )
        except Exception as exc:
            msg = f"Store failed: {exc}"
            print(f"  {prefix} → ERROR: {msg}")
            errors.append({"template": template_name, "error": msg})
            error_count += 1
            continue

        if success:
            in_count = len(fixture["input_extracted"])
            exp_count = len(fixture["expected_normalized"])
            print(f"  {prefix} → stored (input={in_count}, expected={exp_count})")
            ok_count += 1
        else:
            msg = "POST returned non-2xx"
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


if __name__ == "__main__":
    main()
