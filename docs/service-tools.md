# Workflow 1 SQLite service tools

The mock environment serves account and case reads, case edits and closure, stock queries and transfers, work-order creation and cancellation, and simulated delivery. `build_service_registry()` audits the available tool versions and handlers before exposing `registry.invoke(store, reference, arguments, request_key=...)` for trusted callers. Invocation returns a validated observation or a `{code, message}` error. A write result also carries its effect record when available.

`cases.get@1.0.0` returns the case ID and status. `cases.get@2.0.0` adds account ID, issue, notes, and replacement approval. Every write requires a request key. Work-order and delivery verification use that original key to read the stored row. A cancelled order remains visible by its original key, and an open, approved case can then receive a new order under a new key.

## Manual checks

Run these commands from the repository root:

```sh
uv run --locked pytest -v tests/test_service_tools.py
bash scripts/check.sh
```

The replacement test starts with two available units at `north`. It creates one order, moves one unit to reserved, and records one simulated delivery. It repeats the reservation and notification requests and verifies one order, one available unit, one reserved unit, and one delivery. Two fresh SQLite runs produce identical results and final state hashes.

The lost-response test discards the create result, reopens SQLite, and verifies the committed order through `work_orders.get_by_idempotency_key`. The existing feasibility CLI and published replay hashes can be checked using the [state safety guide](state-safety.md).

## Batch evidence

| Tracker | Requirements | Check |
| --- | --- | --- |
| P1-05 | FR-ENV-002 | Account lookup, stored verification, and both case versions return exact data or normalized missing-ID errors. |
| P1-06 | FR-ENV-002, FR-ENV-004 | Case updates reject stale notes, and closure requires an open case. |
| P1-07 | FR-ENV-002 | Inventory reads return exact counts for every fixture row and preserve state. |
| P1-08 | FR-ENV-002, FR-ENV-004 | Stock transfers conserve quantities, reject invalid requests, and apply a repeated key once. |
| P1-09 | FR-ENV-006 | A read by the original key finds the committed work order after a lost response. |
| P1-10 | FR-ENV-002, FR-ENV-004, FR-ENV-006 | Creation enforces the approval cap across different keys. Cancellation applies to created orders. A failed postcondition rolls back. |
| P1-11 | FR-ENV-002, FR-ENV-004 | Repeated sends produce one simulated delivery, and inspection preserves state. |
| P1-12 | FR-ENV-003 | Startup rejects missing contracts and write-based verification paths before creating artifacts. Exact version resolution and result schema validation are covered. |

The next batch adds clean scenario grading, deterministic replay of the replacement scenario, and the M1 evidence bundle.
