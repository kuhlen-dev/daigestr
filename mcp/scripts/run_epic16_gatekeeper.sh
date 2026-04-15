#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MCP_DIR="$ROOT_DIR/mcp"

export DATABASE_URL="${DATABASE_URL:-postgresql://daigestr:daigestr@localhost:15432/daigestr}"

cd "$MCP_DIR"

python3 -m pytest \
  tests/test_execution_db.py \
  tests/test_execution_flow.py \
  tests/test_async_result.py \
  tests/test_audit_api_contract.py \
  tests/test_persistence_health.py \
  tests/test_tips.py \
  tests/test_tips_contract.py \
  tests/test_api_docs_contract.py \
  tests/test_docs_sync_contract.py \
  tests/test_epic16_wave_regressions.py \
  tests/test_epic16_end_to_end_contract.py \
  tests/test_epic16_integrity_axes.py \
  -q --tb=short
