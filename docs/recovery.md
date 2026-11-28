# Recovery benchmark

The recovery runner starts each bundled scenario in a fresh SQLite database. The gateway gives the policy the visible tool reply. The controller uses the scenario's pinned tool versions, budgets, scope, and trusted evidence to decide whether a proposal can run. The verifier checks the tool response and SQLite state before the workflow advances.

## Run the M2 and M3 cases

Run from the repository root:

```sh
uv run --locked toolswap scenario run --manifest examples/recovery/committed-timeout.yaml
uv run --locked toolswap scenario run --manifest examples/recovery/unresolved-timeout.yaml
uv run --locked toolswap scenario run --manifest examples/recovery/schema-repair.yaml
uv run --locked toolswap scenario run --manifest examples/recovery/unknown-case-repair.yaml
```

| Case | CLI outcome | Evidence |
| --- | --- | --- |
| Committed timeout | `PASS`, exit 0 | A create commits, the policy sees a timeout, and a lookup by the original key verifies the order before `ACCEPT`. There is one create dispatch and exactly one order, one reservation, and one delivery. |
| Unresolved timeout | `FAIL`, exit 1 | The lookup times out. The response reason is `verification_inconclusive`. The committed order is preserved, and reservation and notification are pending. |
| Schema repair | `PASS`, exit 0 | The old `notes` field fails validation before dispatch. The repaired call uses `new_notes` and the verified previous note. One update commits and a case read verifies it. |
| Invented case ID | `FAIL`, exit 1 | The guard reports `argument_without_provenance` for `case_id`. The ledger is empty and the final hash equals the initial hash. |

The two cases that return exit 1 test that the controller stops safely. Repeat a command to compare its printed hashes. The artifact directory holds the resolved manifest, trace, effect ledger, snapshots, final response, grading evidence, termination record, and integrity seal. Elapsed times stay outside the canonical evidence.

Set `RUN_DIR` to the path printed on the `Artifacts` line and check the bundle offline.

```sh
uv run --locked toolswap trace verify --run-dir "$RUN_DIR"
```

An intact bundle prints `Integrity: PASS`, including a bundle from a safe stop. To check against a reviewed trace hash, add `--expected-trace-hash` with the digest from the [Workflow 1 gate report](workflow-1-gate.md). The seal checks the contents of a local bundle. The reviewed digest gives you an independent reference when checking a copy.

## Verify the batch

```sh
uv run --locked pytest -q tests/test_recovery_controller.py tests/test_recovery_previews.py tests/test_recovery_evidence.py tests/test_recovery_evaluation.py
bash scripts/check.sh
```

| Tracker | Acceptance evidence |
| --- | --- |
| P2-01 | Versioned previews resolve and reset consistently. An invalid fault point fails before SQLite opens. |
| P2-02 | Versioned calls and observations capture normalized outcomes, retryability, and the trusted request key. |
| P2-03 | The policy boundary rejects malformed, oversized, refused, undeclared, and invalid proposals. A policy cannot alter trusted evidence through the request object. |
| P2-04 | Tests exercise all eight actions, including safe stops and non-mutating handoffs. |
| P2-05 | Exact budget boundaries block the next call. Changing a write key leaves the equivalent-call count intact. |
| P2-06 | The repair uses per-field provenance from the task and verified read. An invented ID never reaches the update handler. |
| P2-07 | A transient read may retry. A write retry uses the same key after affirmative no-effect evidence. The guard blocks retry and acceptance after an ambiguous commit. |
| P2-08 | The guard checks permission, resource, target count, and effect limits. Confirmation reports the exact proposed effect. |
| P2-09 | Fallbacks must match the declared capability and freshness. Information and confirmation handoffs record the pending operation. |
| P2-10 | Guard records identify the rule, scoped call, evidence, and reason. |
| P2-11 | The verifier checks the pinned result, matching effect record, and SQLite row. Contradictory results and missing evidence fail. |
| P2-12 | The original key verifies the committed order before continuation. An inconclusive lookup prevents dependent writes. |

The [Workflow 1 gate report](workflow-1-gate.md) covers P2-13 through P2-20 and pins the formal M2 and M3 results.
