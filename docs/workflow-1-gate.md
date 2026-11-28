# Workflow 1 gate report v1.0.0

Workflow 1 passed the local closeout gate at 53 of 53 tracker tasks. The four bundled cases use the scripted recovery policy, seed 0, and mock service operations. Each case regenerates an identical evidence bundle in a separate workspace with the same manifest settings.

## M2 and M3 results

| Manifest under `examples/recovery/` | Exit | Verified outcome |
| --- | --- | --- |
| `committed-timeout.yaml` | 0 | The create commits once. A lookup by `replacement-order` matches the order and effect before `ACCEPT`. One order, one reservation, and one delivery match the clean M1 state. |
| `unresolved-timeout.yaml` | 1 | The original order remains. `verification_inconclusive` ends the run before any retry, reservation, or delivery. |
| `schema-repair.yaml` | 0 | The gateway rejects the stale `notes` field before dispatch. One repaired update uses `new_notes`, and a read verifies the saved note before `ACCEPT`. |
| `unknown-case-repair.yaml` | 1 | The guard records `argument_without_provenance` for `case_id`. There are zero update dispatches and effects. Initial and final state match. |

The replay tests pin these SHA-256 digests for the bundled manifests.

| Case | State hash after `sha256:` | Trace hash after `sha256:` |
| --- | --- | --- |
| Committed timeout | `45553771dd751776c18dbc8fdebe512ec8bd576a38666193433cb028e7e17e39` | `3dff0e21f1c57ed218b53a487ddaad3ffc691eac0d3b460718279d947874de04` |
| Unresolved timeout | `54669147ac79fb43290e97467fb0cc595f7ddc1f0884f92fb3f4fa3f0818dd77` | `77a8acedcb828832a6d475256be19e8f5576c9cba1d1128e0fea26a54d6da554` |
| Schema repair | `03ede11132039d5ffd2cc1f31547b55eabe312c1240c030d9e38d406c8fa6797` | `403db9926f8a2ce7c03391fffed7f89d22a915ac3e99c8307760e78f064e46ab` |
| Invented case ID | `010afeb5899c67b445b21b37d8c1c5aa7760c7cd6b384e8412dd4978a36213ad` | `da0cf0773751903f57237a36ea8de548727f63dbcb193fd484ae454da85f6eeb` |

## Frozen interfaces

- The policy receives `PolicyInput@1.0.0` and emits `RecoveryDecision@1.0.0` bytes or an explicit refusal. The controller owns scope, budgets, exact call authorization, and transitions. The gate scenarios are pinned at version `1.0.0`, with `cases.update@2.0.0` used by M3.
- The gateway records a `ToolCall@1.0.0` before validation and marks a dispatch immediately before registry invocation. `Observation@1.0.0` contains the policy-visible outcome. SQLite effect and fault evidence stays with the controller and evaluator.
- `Verification@1.0.0` binds postconditions to the exact tool and evidence. `Termination@1.0.0` records the reason and pending step. `MetricReport@1.0.0` owns the scenario result and its assertion and safety evidence.
- Recovery artifact format `2.0.0` uses canonical `Event@1.0.0` JSONL, a SHA-256 chain, and content-addressed snapshots. `integrity.json` seals the bundle. The format version separates these runs from the historical previews. M1 and feasibility retain their published hashes.

## Acceptance evidence

| Task | Check |
| --- | --- |
| P2-13 | `test_gate_cli_replays_deterministically` checks canonical bytes and verifies dispatch records against calls observed at the registry. |
| P2-14 | `test_offline_verify_rejects_tampered_bundles` checks event ordering, truncation, snapshots, effect linkage, and outcome files. Offline verification also checks an external expected digest without opening SQLite. |
| P2-15 | Termination tests cover expected stops and SQLite errors before and after commit. A failed infrastructure attempt has its own evidence directory, so a fresh rerun can publish the normal result. |
| P2-16 | Evaluation tests check missing state and postconditions, misleading replies, unauthorized dispatch evidence, and duplicate commits. |
| P2-17 | Controller security tests instrument the registry and inspect the ledger. Malformed proposals, invented tools and arguments, retry and scope violations, repeated calls past budget, and instruction-like tool content cause zero prohibited dispatches. |
| P2-18–P2-19 | The gate cases verify chronology and final state as listed above. The two expected safe stops pass offline bundle verification. |
| P2-20 | The full quality pipeline and pinned M1 and feasibility replays pass locally. |

## Manual review

Run the four commands in [Recovery benchmark](recovery.md). For each run, substitute the printed artifact directory and the appropriate digest from this report, including its `sha256:` prefix.

```sh
uv run --locked toolswap trace verify --run-dir "$RUN_DIR" --expected-trace-hash "$TRACE_HASH"
bash scripts/check.sh
```

Scenario exit `0` means pass, `1` means graded failure, `2` means invalid input, and `3` means infrastructure or integrity failure. Trace verification returns `0` for an intact bundle, `2` for invalid command input, and `3` for a missing, unreadable, or inconsistent bundle. A started run can retain its infrastructure-failure evidence when the artifact directory is writable.
