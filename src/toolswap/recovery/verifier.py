"""Resolve tool outcomes using pinned postconditions and read-only SQLite evidence."""

import json

from toolswap.contracts import JsonObject
from toolswap.determinism import canonical_json
from toolswap.recovery.gateway import Dispatch, Gateway
from toolswap.recovery.records import Step, Verification, VerificationStatus


def matches(expected: JsonObject, observed: JsonObject | None) -> bool:
    return observed is not None and all(
        key in observed and canonical_json(value) == canonical_json(observed[key])
        for key, value in expected.items()
    )


def _record(
    gateway: Gateway,
    attempt: Dispatch,
    probe: Dispatch | None,
    status: VerificationStatus,
    evidence: JsonObject,
) -> Verification:
    call = attempt.visible.call
    return Verification(
        status=status,
        tool=call.tool,
        postconditions=gateway.registry.contract(call.tool).postconditions,
        call_id=call.call_id,
        probe_call_id=probe.visible.call.call_id if probe is not None else None,
        state_hash=gateway.store.current_state().state_hash,
        evidence=evidence,
    )


def _no_effect(gateway: Gateway, attempt: Dispatch) -> bool:
    call = attempt.visible.call
    if attempt.before != attempt.after or gateway.store.current_state().state_hash != attempt.after:
        return False
    effect = gateway.store.lookup_effect(call.request_key) if call.request_key is not None else None
    actual = attempt.actual.effect if attempt.actual is not None else None
    if effect is None:
        return actual is None and (call.request_key is None or attempt.timeout != "after")
    return (
        effect == actual
        and effect.tool == call.tool
        and effect.status == "not_run"
        and effect.state_before_hash == effect.state_after_hash == attempt.before
    )


def verify(
    gateway: Gateway, step: Step, attempt: Dispatch, probe: Dispatch | None = None
) -> Verification:
    call = attempt.visible.call
    if call.tool != step.tool and all(item.tool != call.tool for item in step.alternatives):
        return _record(gateway, attempt, probe, "inconclusive", {"reason": "tool_version_mismatch"})
    visible = attempt.visible if probe is None else probe.visible
    if probe is not None and (
        step.probe is None
        or visible.call.tool != step.probe.tool
        or canonical_json(visible.call.arguments) != canonical_json(step.probe.arguments)
        or probe.before != probe.after
    ):
        return _record(
            gateway,
            attempt,
            probe,
            "inconclusive",
            {"reason": "verification_read_failed", "error": visible.error},
        )
    if visible.error is not None:
        known_absent = (probe is None or visible.error.get("code") == "not_found") and _no_effect(
            gateway, attempt
        )
        return _record(
            gateway,
            attempt,
            probe,
            "not_applied" if known_absent else "inconclusive",
            {
                "reason": "tool_error" if probe is None else "verification_read_failed",
                "error": visible.error,
                "entered_environment": attempt.actual is not None,
            },
        )
    if not matches(step.expected, visible.result):
        return _record(
            gateway,
            attempt,
            probe,
            "inconclusive",
            {
                "reason": "observation_mismatch",
                "expected": step.expected,
                "observed": visible.result,
            },
        )
    if call.request_key is None:
        return _record(
            gateway,
            attempt,
            probe,
            "verified",
            {"expected": step.expected, "observed": visible.result},
        )

    effect = gateway.store.lookup_effect(call.request_key)
    state_hash = gateway.store.current_state().state_hash
    if (
        effect is None
        or effect.tool != call.tool
        or effect.status != "complete"
        or effect.state_before_hash != attempt.before
        or effect.state_after_hash != state_hash
        or visible.result is None
        or not matches(visible.result, effect.result)
        or step.table is None
        or step.expected_row is None
    ):
        return _record(
            gateway, attempt, probe, "inconclusive", {"reason": "effect_evidence_mismatch"}
        )
    snapshot = json.loads(gateway.store.resolve_snapshot(state_hash))
    # The original request and pinned postcondition identify the row. Bind any returned
    # entity ID to that same row so a response for a different record cannot pass.
    identity: JsonObject = {
        key: visible.result[key]
        for key in ("case_id", "work_order_id", "message_id", "sku", "location")
        if key in visible.result
    }
    rows = [
        row
        for row in snapshot["tables"][step.table]
        if matches(step.expected_row, row) and matches(identity, row)
    ]
    if len(rows) != 1:
        return _record(
            gateway, attempt, probe, "inconclusive", {"reason": "state_evidence_mismatch"}
        )
    return _record(
        gateway,
        attempt,
        probe,
        "verified",
        {"effect_id": effect.effect_id, "expected": step.expected_row, "row": rows[0]},
    )
