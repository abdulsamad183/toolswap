# Workflow 1 SQLite state safety

The state store gives each mock service-operations run its own SQLite database. Reset creates a fresh database in a caller-owned directory using the numbered migration and the fixture's declared snapshot hash. Run metadata and request history stay outside the business-state hash.

For a trusted write, the store commits the updated business rows, before and after snapshots, effect record, and request-key registration together. The lookup opens the database again and returns the recorded outcome for the key. An exact repeat after a completed write returns the original result and adds a `duplicate` record with an empty state diff. A repeat after a partial write returns the partial evidence without dispatching another write.

## Manual checks

Run from the repository root with the locked environment:

```sh
uv run --locked pytest -v tests/test_state_safety.py
bash scripts/check.sh
```

The focused suite uses temporary databases and trusted test writes. The lost-response test discards the first result, reopens the database, and checks for one committed effect and one work order. The concurrent request test starts two writes with the same key and checks for one `complete` record and one `duplicate` record.

Run the read-only feasibility scenario twice to check compatibility:

```sh
uv run --locked toolswap scenario run --manifest examples/feasibility/run.yaml
uv run --locked toolswap scenario run --manifest examples/feasibility/run.yaml
```

Both runs should show `Outcome: PASS` with state hash `sha256:96c900dc6535a52ee65115d08a0fbb981cf1d5c70bee14b336b1947802174f48` and trace hash `sha256:0e41cfd7ff5eab0b2def21fa697e50a26b67fb835026308903d9c7567e78237a`.

## Batch evidence

| Tracker | Requirements | Checks |
| --- | --- | --- |
| P1-01 | FR-ENV-002 | `test_fresh_migrations_and_resets_are_exact` checks schema version 1 and byte equality on one runtime. `test_database_enforces_domain_constraints` checks foreign keys, uniqueness, and stock constraints. |
| P1-02 | FR-ENV-001, NFR-002 | `test_fresh_migrations_and_resets_are_exact` checks snapshot and effect determinism. `test_reset_requires_declared_canonical_snapshot` checks checksum, canonical JSON, invalid seed data, and an intentionally revised fixture. |
| P1-03 | FR-REC-007 | `test_write_has_resolvable_minimal_state_diff` checks exact row and field changes. `test_read_does_not_change_state_or_effects` checks the artifact hash and an empty diff. `test_snapshot_corruption_is_detected_on_read` checks checksum enforcement. |
| P1-04 | FR-ENV-004, FR-FLT-004 | The effect tests cover all four statuses, conflicting keys, lost responses, rollback after a late failure, and parallel suppression. |

The next service-tools batch connects these primitives to account, case, inventory, work-order, and notification operations.
