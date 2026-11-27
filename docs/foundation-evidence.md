# Workflow 1 foundation evidence

| Tracker | PRD coverage | Acceptance evidence |
| --- | --- | --- |
| P0-01 | PRD 3.1–3.3, NFR-003 | Release scope and non-goals in the foundation decisions. |
| P0-02 | PRD 3.4, 11.2, RC-01–RC-11 | Metrics, safety invariants, release gates, and worked examples in the foundation decisions. |
| P0-03 | NFR-007, PRD 1.1 | Python 3.12, uv, Apache-2.0, and repository boundaries. CLI installation and help are exercised by the quality suite. |
| P0-04 | NFR-001, PRD 17 | `bash scripts/check.sh` runs lint, formatting, typing, tests, README command checks, coverage, and lockfile hashing. The CI matrix targets macOS and Linux. |
| P0-05 | NFR-010 | Compatibility policy and a test for rejecting an unsupported wire version. |
| P0-06 | PRD 14.3, NFR-001 | Tests for canonical defaults, equivalent YAML, strict input, and environment-variable isolation. |
| P0-07 | FR-ENV-003 | Tool contract round-trip, schema validation, and field-specific errors for each missing mandatory field. |
| P0-08 | FR-REC-001, FR-REC-002, PRD 7.3 | Scenario and recovery decision tests cover round-trips, invalid fields, budgets, and all eight actions. |
| P0-09 | FR-REC-006, FR-REC-007, FR-EVAL-005 | Versioned artifact record validation. |
| P0-10 | NFR-002 | Canonical serialization, stable hashes and IDs, seeded streams, and logical clock tests. |
| P0-11 | NFR-004, PRD 16 | Allowlist, inert proposal text, artifact path rejection, and CLI diagnostic tests. |
