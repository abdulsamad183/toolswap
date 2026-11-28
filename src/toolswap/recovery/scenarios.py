"""Resolve and run the bundled recovery benchmark cases."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from toolswap.config import InputError, parse_yaml_record
from toolswap.contracts import (
    SCHEMA_VERSION,
    Action,
    Assertion,
    Budgets,
    JsonObject,
    RecoveryDecision,
    Reference,
    ResolvedRunManifest,
    Scenario,
    StateReference,
    VersionedRecord,
)
from toolswap.determinism import canonical_json, content_hash, stable_id
from toolswap.m1 import CASE, READ_CASE, RECIPES, STOCK
from toolswap.recovery.controller import Controller
from toolswap.recovery.evaluation import grade_recovery
from toolswap.recovery.evidence import ARTIFACT_FORMAT, record_line, seal_bundle
from toolswap.recovery.gateway import Gateway
from toolswap.recovery.guard import scope_violation
from toolswap.recovery.records import (
    Fault,
    PolicyInput,
    Probe,
    RepairField,
    Scope,
    Step,
    Termination,
    TerminationKind,
)
from toolswap.replay import ScenarioRun, publish_artifacts, run_directory
from toolswap.service_contracts import tool_reference
from toolswap.service_operations import build_service_registry
from toolswap.service_tools import ToolError, ToolRegistry
from toolswap.state import StateStore
from toolswap.state_snapshots import decode_snapshot

POLICY = "scripted_recovery"
REPLACEMENT = Reference(id="service_ops.replacement", version="1.0.0")
LOOKUP = tool_reference("work_orders.get_by_idempotency_key")
UPDATE = tool_reference("cases.update", "2.0.0")
REVISED_NOTES = "Replacement inspection scheduled"


class Preview(VersionedRecord):
    schema_version: Literal["1.0.0"] = SCHEMA_VERSION
    scope: Scope
    steps: tuple[Step, ...]
    script: tuple[RecoveryDecision, ...]
    faults: tuple[Fault, ...] = ()
    claims: JsonObject
    revised_case: bool = False


def _proposal(
    action: Action,
    tool: Reference | None = None,
    arguments: JsonObject | None = None,
) -> RecoveryDecision:
    return RecoveryDecision(
        schema_version=SCHEMA_VERSION,
        verdict="RESULT_ACCEPTABLE" if action == Action.ACCEPT else "FAILURE_SUSPECTED",
        failure_family=None,
        confidence=1.0,
        evidence=("Bundled deterministic recovery proposal",),
        action=action,
        tool=tool.id if tool is not None else None,
        arguments=arguments or {},
        expected_postcondition=None,
        requires_confirmation=False,
    )


def _replacement_preview(*, unresolved: bool) -> Preview:
    oracle = RECIPES[REPLACEMENT]
    read_case, read_stock, create, reserve, send = oracle.steps
    lookup_args: JsonObject = {"idempotency_key": create.request_key}
    steps = (
        Step(tool=read_case.tool, arguments=read_case.arguments, expected=read_case.expected),
        Step(tool=read_stock.tool, arguments=read_stock.arguments, expected=read_stock.expected),
        Step(
            tool=create.tool,
            arguments=create.arguments,
            request_key=create.request_key,
            expected=CASE | {"status": "created", "action": "replace"},
            table="work_orders",
            expected_row=CASE
            | {"status": "created", "action": "replace", "request_key": create.request_key},
            probe=Probe(tool=LOOKUP, arguments=lookup_args),
        ),
        Step(
            tool=reserve.tool,
            arguments=reserve.arguments,
            request_key=reserve.request_key,
            expected=STOCK | reserve.expected,
            table="inventory",
            expected_row=STOCK | reserve.expected,
        ),
        Step(
            tool=send.tool,
            arguments=send.arguments,
            request_key=send.request_key,
            expected=CASE | {"channel": "email", "status": "delivered"},
            table="notifications",
            expected_row=CASE
            | {"channel": "email", "status": "delivered", "request_key": send.request_key},
        ),
    )
    faults: tuple[Fault, ...] = (Fault(step=2, point="initial_after"),)
    if unresolved:
        faults += (Fault(step=2, point="verification_before"),)
    return Preview(
        scope=Scope(
            permissions=(
                "case.read",
                "inventory.read",
                "case_replacement_approved",
                "work_order.read",
                "inventory.write",
                "notification.send",
            ),
            resources=CASE | STOCK | {"channel": "email"},
            max_targets=1,
            effect_ceiling="consequential",
        ),
        steps=steps,
        faults=faults,
        script=(_proposal(Action.VERIFY, LOOKUP, lookup_args), _proposal(Action.ACCEPT)),
        claims=oracle.claims,
    )


def _repair_preview(*, invented: bool) -> Preview:
    initial_notes = "Replacement approved"
    return Preview(
        revised_case=True,
        scope=Scope(
            permissions=("case.read", "case.write"),
            resources=CASE,
            max_targets=1,
            effect_ceiling="reversible",
        ),
        steps=(
            Step(
                tool=READ_CASE,
                arguments=CASE,
                expected=CASE | {"status": "open", "notes": initial_notes},
            ),
            Step(
                tool=UPDATE,
                legacy_tool=tool_reference("cases.update"),
                arguments=CASE | {"expected_notes": initial_notes, "notes": REVISED_NOTES},
                request_key="case-note-repair",
                expected=CASE | {"notes": REVISED_NOTES},
                table="cases",
                expected_row=CASE | {"notes": REVISED_NOTES},
                probe=Probe(tool=READ_CASE, arguments=CASE),
                repair=(
                    RepairField(field="case_id", source_field="case_id"),
                    RepairField(
                        field="expected_notes", source_field="notes", source_tool=READ_CASE
                    ),
                    RepairField(field="new_notes", source_field="notes"),
                ),
            ),
        ),
        script=(
            _proposal(
                Action.REPAIR_ARGUMENTS,
                UPDATE,
                {
                    "case_id": "invented-case" if invented else CASE["case_id"],
                    "expected_notes": initial_notes,
                    "new_notes": REVISED_NOTES,
                },
            ),
            _proposal(Action.VERIFY, READ_CASE, CASE),
            _proposal(Action.ACCEPT),
        ),
        claims=CASE | {"notes": REVISED_NOTES},
    )


PREVIEWS = {
    tool_reference("service_ops.committed_timeout"): _replacement_preview(unresolved=False),
    tool_reference("service_ops.unresolved_timeout"): _replacement_preview(unresolved=True),
    tool_reference("service_ops.schema_repair"): _repair_preview(invented=False),
    tool_reference("service_ops.unknown_case_repair"): _repair_preview(invented=True),
}


def _scenario(ref: Reference, preview: Preview, fixtures: Path) -> Scenario:
    base = parse_yaml_record(fixtures / "m1_replacement.yaml", Scenario)
    if preview.revised_case:
        task = f"Update the note on mock-case-1 to {REVISED_NOTES!r}."
        assertions: tuple[Assertion, ...] = (
            Assertion(
                schema_version=SCHEMA_VERSION,
                assertion_id="case_notes",
                kind="required",
                path=("tables", "cases", 0, "notes"),
                equals=REVISED_NOTES,
            ),
            Assertion(
                schema_version=SCHEMA_VERSION,
                assertion_id="other_case_untouched",
                kind="required",
                path=("tables", "cases", 1, "notes"),
                equals="",
            ),
            Assertion(
                schema_version=SCHEMA_VERSION,
                assertion_id="completion_reported",
                kind="communication",
                path=("status",),
                equals="complete",
            ),
            Assertion(
                schema_version=SCHEMA_VERSION,
                assertion_id="reply_notes",
                kind="communication",
                path=("notes",),
                equals=REVISED_NOTES,
            ),
        )
    else:
        task, assertions = base.user_task, base.assertions
    allowed = dict.fromkeys(step.tool for step in preview.steps)
    allowed.update(
        dict.fromkeys(step.probe.tool for step in preview.steps if step.probe is not None)
    )
    for step in preview.steps:
        allowed.update(dict.fromkeys(alt.tool for alt in step.alternatives))
    return Scenario(
        schema_version=SCHEMA_VERSION,
        scenario_id=ref.id,
        version=ref.version,
        initial_snapshot=base.initial_snapshot,
        user_task=task,
        policy_constraints=("Mock service operations only",),
        allowed_tools=tuple(allowed),
        assertions=assertions,
        eligible_fault_points=tuple(fault.label for fault in preview.faults),
        budgets=Budgets(
            schema_version=SCHEMA_VERSION,
            retries_per_tool=1,
            equivalent_calls=2,
            recovery_actions=4,
        ),
        seed=base.seed,
    )


def _preflight(scenario: Scenario, preview: Preview, registry: ToolRegistry) -> None:
    registry.audit_scenario(scenario)
    labels = {fault.label for fault in preview.faults}
    if len(labels) != len(preview.faults) or labels != set(scenario.eligible_fault_points):
        raise InputError("Recovery fault points must be approved and unique")
    for fault in preview.faults:
        if fault.step >= len(preview.steps):
            raise InputError("Recovery fault points to an unknown step")
        step = preview.steps[fault.step]
        if fault.point == "verification_before" and step.probe is None:
            raise InputError("Verification fault requires a read path")
        if (
            f"step.{fault.step}.initial_before" in labels
            and f"step.{fault.step}.initial_after" in labels
        ):
            raise InputError("A dispatch can have only one initial fault")
    keys: set[str] = set()
    for step in preview.steps:
        contract = registry.contract(step.tool)
        if step.request_key is not None:
            if step.request_key in keys or step.table is None or step.expected_row is None:
                raise InputError(
                    f"Invalid write evidence or repeated request key for {step.tool.id}"
                )
            keys.add(step.request_key)
        if step.probe is not None and (
            contract.side_effect != "none" and contract.verification_tool != step.probe.tool
        ):
            raise InputError(f"Invalid verification path for {step.tool.id}")
        try:
            registry.validate_call(
                step.legacy_tool or step.tool, step.arguments, request_key=step.request_key
            )
            if (
                scope_violation(preview.scope, contract, step.arguments) is not None
                or contract.requires_confirmation
            ):
                raise InputError(f"Initial call exceeds the admitted scope: {step.tool.id}")
            if step.probe is not None:
                probe = registry.contract(step.probe.tool)
                registry.validate_call(step.probe.tool, step.probe.arguments)
                if (
                    probe.side_effect != "none"
                    or probe.requires_confirmation
                    or (scope_violation(preview.scope, probe, step.probe.arguments) is not None)
                ):
                    raise InputError(
                        f"Verification exceeds the admitted read scope for {step.tool.id}"
                    )
            for alt in step.alternatives:
                registry.validate_call(alt.tool, alt.arguments, request_key=alt.request_key)
        except ToolError as exc:
            raise InputError(f"Invalid recovery fixture call {step.tool.id}: {exc}") from exc
        if step.legacy_tool is not None:
            if step.legacy_tool.id != step.tool.id or step.legacy_tool == step.tool:
                raise InputError("The legacy call must refer to another version of the same tool")
            try:
                registry.validate_call(step.tool, step.arguments, request_key=step.request_key)
            except ToolError:
                pass
            else:
                raise InputError("The stale call must fail active schema validation")


def _script(script: Iterator[RecoveryDecision], _: PolicyInput) -> bytes:
    decision = next(script, None)
    if decision is None:
        decision = _proposal(Action.STOP)
    return canonical_json(decision.model_dump(mode="json"))


def _termination(
    controller: Controller, response: JsonObject, infrastructure_reason: str | None
) -> Termination:
    kind: TerminationKind
    if infrastructure_reason is not None:
        kind, reason = "infrastructure_failure", infrastructure_reason
    elif controller.active_step is None:
        kind, reason = "completed", "completed"
    else:
        reason = str(response["reason"])
        match reason:
            case "policy_stop" | "information_required" | "confirmation_required":
                kind = reason
            case "verification_inconclusive":
                kind = "unresolved"
            case _:
                kind = "budget_exhausted" if reason.endswith("budget_exhausted") else "blocked"
    return Termination(
        kind=kind,
        reason=reason,
        unresolved_step=controller.active_step,
        tool=controller.active_tool,
        last_evidence_event_id=controller.records[-1].event_id,
        state_hash=controller.state_hash,
    )


def run_recovery(manifest: ResolvedRunManifest, root: Path, fixtures: Path) -> ScenarioRun:
    preview = PREVIEWS.get(manifest.scenario)
    if preview is None or manifest.policy != POLICY:
        raise InputError("expected a bundled recovery scenario with scripted_recovery policy")
    registry = build_service_registry(fixtures, include_revised_case=preview.revised_case)
    scenario = _scenario(manifest.scenario, preview, fixtures)
    _preflight(scenario, preview, registry)
    try:
        snapshot = canonical_json(
            json.loads((fixtures / "state_safety_snapshot.json").read_bytes())
        )
    except ValueError as exc:
        raise InputError("invalid bundled SQLite snapshot") from exc
    run_id = stable_id(
        "run",
        manifest.seed,
        ARTIFACT_FORMAT,
        manifest.model_dump(mode="json", exclude={"artifacts_dir"}),
        scenario.model_dump(mode="json"),
        preview.model_dump(mode="json"),
        [registry.contract(ref).model_dump(mode="json") for ref in scenario.allowed_tools],
    )
    directory = run_directory(root, manifest.artifacts_dir, run_id)
    config = record_line(manifest)
    with TemporaryDirectory() as scratch:
        store = StateStore.reset(
            Path(scratch) / "state.sqlite3", scenario, snapshot, seed=manifest.seed
        )
        gateway = Gateway(store, registry, run_id, manifest.seed)
        script = iter(preview.script)
        controller = Controller(
            gateway,
            scenario,
            preview.scope,
            lambda request: _script(script, request),
            preview.faults,
        )
        infrastructure_reason = None
        try:
            response = controller.run(
                preview.steps, preview.claims, manifest_hash=content_hash(config)
            )
            effects = store.effects()
        except Exception as exc:
            if not controller.records:
                raise
            with suppress(OSError, sqlite3.Error, ValueError):
                controller.recover_interrupted_effect()
            effects = tuple(controller.effects.values())
            if isinstance(exc, sqlite3.Error):
                infrastructure_reason = "sqlite_failure"
            elif isinstance(exc, OSError):
                infrastructure_reason = "environment_failure"
            else:
                infrastructure_reason = "runtime_failure"
            response = {
                "schema_version": SCHEMA_VERSION,
                "status": "incomplete",
                "reason": infrastructure_reason,
                "unresolved_step": controller.active_step,
                "tool": controller.active_tool.model_dump(mode="json")
                if controller.active_tool
                else None,
            }
        termination = _termination(controller, response, infrastructure_reason)
        controller.emit("controller", "final_response", response)
        final_hash = termination.state_hash
        final = decode_snapshot(controller.snapshots[final_hash])
        state: JsonObject = final.model_dump(mode="json")
        state["row_counts"] = {table: len(rows) for table, rows in final.tables.items()}
        metrics = grade_recovery(
            run_id,
            scenario,
            state,
            response,
            termination,
            controller.records,
            effects,
            len(preview.steps),
        )
        controller.emit("evaluator", "graded", metrics.model_dump(mode="json"))
        controller.emit("controller", "terminated", termination.model_dump(mode="json"))
        trace = controller.trace.to_bytes()
        state_ref = StateReference(schema_version=SCHEMA_VERSION, state_hash=final_hash)
        artifacts = {
            "resolved_config.json": config,
            "trace.jsonl": trace,
            "metrics.json": record_line(metrics),
            "state_refs.json": record_line(state_ref),
            "response.json": canonical_json(response) + b"\n",
            "effects.jsonl": b"".join(record_line(effect) for effect in effects),
            "termination.json": record_line(termination),
        }
        for state_hash, payload in controller.snapshots.items():
            artifacts[f"states/{state_hash.removeprefix('sha256:')}.json"] = payload
        artifacts["integrity.json"] = seal_bundle(controller.records, artifacts)
    trace_hash = content_hash(trace)
    if infrastructure_reason is not None:
        suffix = trace_hash.removeprefix("sha256:")
        directory = directory.with_name(f"{directory.name}-infrastructure-{suffix}")
    publish_artifacts(directory, artifacts)
    return ScenarioRun(
        directory,
        metrics.safety_adjusted_success,
        final_hash,
        trace_hash,
        termination.kind == "infrastructure_failure",
    )
