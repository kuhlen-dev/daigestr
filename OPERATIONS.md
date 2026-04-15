# Daigestr Operations

This document is the operator-oriented runtime guide for the current Epic 16 execution model.

## Runtime Model

- Direct requests return the final `ConvertResponse` immediately.
- Async requests create one canonical execution plus one compatibility `job_id`.
- Batches create one persisted batch plus one canonical `execution_kind=batch_item` execution per item.
- Replay endpoints always create a new canonical `execution_kind=replay` execution.

Canonical polling and result retrieval:

- `GET /v1/executions/{execution_id}` is the source of truth for status, progress, attempts, subjobs, and lightweight result summary.
- `GET /v1/executions/{execution_id}/result` returns the persisted final payload when `final_result_available=true`.
- `GET /v1/batches/{batch_id}` is the aggregate batch surface.
- `GET /v1/batches/{batch_id}/items` is the per-item lightweight history surface.
- `GET /v1/batches/{batch_id}/items/{batch_item_id}/result` returns the persisted final payload for one batch item.
- `GET /v1/jobs/{job_id}` and `/v1/jobs/{job_id}/result` remain compatibility surfaces for async callers.

## Operator Flags

- `AUDIT_API_ENABLED` gates `/v1/audit/*`.
- `DEBUG_SNAPSHOT_API_ENABLED` gates `/v1/debug/snapshots*`.
- `REPLAY_API_ENABLED` gates execution, batch-item, and snapshot replay endpoints.

When the audit sink is enabled, replay, snapshot export access, and batch-item control actions emit `operator_action` audit events.

## Retention

- `EXECUTION_RESULT_RETENTION_DAYS`: full persisted result payloads
- `EXECUTION_RESULT_ARTIFACT_RETENTION_DAYS`: artifact refs such as enriched PDFs
- `DEBUG_SNAPSHOTS_RETENTION_DAYS`: debug/replay snapshots
- `PII_STORAGE_MODE`: payload storage policy
- `DEBUG_SNAPSHOTS_ALLOW_PII`: opt-in to keep sensitive payload branches in snapshots

Execution lineage metadata persists independently from result payload cleanup.

## Diagnostics

- `GET /v1/health` exposes readiness, retention policy, PII policy, and operator boundary state.
- `GET /v1/diagnostics/executions` exposes active executions, stuck executions, and `normalizer_drift`.
- `GET /v1/tips` and MCP `get_tips` are the normative machine-readable contract for agents and integrators.

## Recommended Verification Loop

1. Start or rebuild the stack with `docker compose up -d --build`.
2. Verify `curl http://localhost:18006/v1/health`.
3. For async and batch paths, poll the canonical execution or batch surfaces instead of parsing logs.
4. Use replay only when the relevant operator flags are enabled.
