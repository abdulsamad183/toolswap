"""Execute the bundled clean SQLite scenarios through their fixed oracle steps."""

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory

from toolswap.config import InputError, parse_yaml_record
from toolswap.contracts import (
    SCHEMA_VERSION,
    EffectRecord,
    JsonObject,
    Reference,
    ResolvedRunManifest,
    Scenario,
    StateReference,
)
from toolswap.determinism import canonical_json, content_hash, stable_id
from toolswap.grading import grade_outcome
from toolswap.replay import (
    ArtifactError,
    ScenarioRun,
    TraceRecord,
    build_trace,
    publish_artifacts,
    run_directory,
)
from toolswap.service_contracts import tool_reference
from toolswap.service_operations import build_service_registry
from toolswap.service_tools import ToolError, ToolRegistry, ToolResult
from toolswap.state import StateStore
from toolswap.state_snapshots import decode_snapshot


@dataclass(frozen=True)
class OracleStep:
    tool: Reference
    arguments: JsonObject
    request_key: str | None = None
    expected: JsonObject = field(default_factory=dict)

    def record(self) -> JsonObject:
        return {
            "tool": self.tool.model_dump(mode="json"),
            "arguments": self.arguments,
            "request_key": self.request_key,
            "expected_observation": self.expected,
        }


@dataclass(frozen=True)
class OracleRecipe:
    fixture: str
    steps: tuple[OracleStep, ...]
    claims: JsonObject


CASE: JsonObject = {"case_id": "mock-case-1"}
STOCK: JsonObject = {"sku": "mock-part-1", "location": "north"}
READ_CASE = tool_reference("cases.get", "2.0.0")
RECIPES = {
    Reference(id="service_ops.inspection", version="1.0.0"): OracleRecipe(
        "m1_inspection.yaml",
        (
            OracleStep(
                READ_CASE, CASE, expected={"status": "open", "account_id": "mock-account-1"}
            ),
            OracleStep(
                tool_reference("accounts.verify"),
                {"account_id": "mock-account-1"},
                expected={"verified": True},
            ),
        ),
        {"case_id": "mock-case-1", "case_status": "open", "account_verified": True},
    ),
    Reference(id="service_ops.replacement", version="1.0.0"): OracleRecipe(
        "m1_replacement.yaml",
        (
            OracleStep(READ_CASE, CASE, expected={"status": "open", "replacement_approved": True}),
            OracleStep(
                tool_reference("inventory.get_availability"), STOCK, expected={"available_count": 2}
            ),
            OracleStep(
                tool_reference("work_orders.create"),
                CASE | {"action": "replace"},
                "replacement-order",
                {"status": "created"},
            ),
            OracleStep(
                tool_reference("inventory.reserve"),
                STOCK | {"quantity": 1},
                "replacement-stock",
                {"available_count": 1, "reserved_count": 1},
            ),
            OracleStep(
                tool_reference("notifications.send"),
                CASE | {"channel": "email"},
                "replacement-email",
                {"status": "delivered"},
            ),
        ),
        {
            "case_id": "mock-case-1",
            "replacement_created": True,
            "reserved_quantity": 1,
            "notification_status": "delivered",
        },
    ),
}


def _preflight(scenario: Scenario, recipe: OracleRecipe, registry: ToolRegistry) -> None:
    registry.audit_scenario(scenario)
    if (
        scenario.eligible_fault_points
        or scenario.budgets.retries_per_tool
        or scenario.budgets.recovery_actions
    ):
        raise InputError("clean scenario requires zero fault and recovery steps")
    if not {"required", "communication"}.issubset(
        assertion.kind for assertion in scenario.assertions
    ):
        raise InputError("clean scenario requires state and communication assertions")
    keys: set[str] = set()
    calls: Counter[tuple[Reference, bytes]] = Counter()
    for step in recipe.steps:
        if step.tool not in scenario.allowed_tools:
            raise InputError(f"undeclared oracle tool {step.tool.id}@{step.tool.version}")
        contract = registry.contract(step.tool)
        if contract.requires_confirmation:
            raise InputError(f"oracle tool requires confirmation: {step.tool.id}")
        try:
            registry.validate_call(step.tool, step.arguments, request_key=step.request_key)
        except ToolError as exc:
            raise InputError(f"invalid oracle call {step.tool.id}: {exc}") from exc
        if step.request_key is not None:
            if contract.side_effect == "none" or step.request_key in keys:
                raise InputError(f"invalid or repeated oracle request key for {step.tool.id}")
            keys.add(step.request_key)
        equivalent_call = (step.tool, canonical_json(step.arguments))
        calls[equivalent_call] += 1
        if calls[equivalent_call] > scenario.budgets.equivalent_calls:
            raise InputError("oracle call exceeds the equivalent-call budget")


def _check_step_evidence(step: OracleStep, result: ToolResult, before: str, after: str) -> None:
    effect = result.effect
    if step.request_key is None or result.error is not None:
        if before != after or effect is not None:
            raise ArtifactError(f"unexpected effect for {step.tool.id}")
    elif (
        effect is None
        or effect.status != "complete"
        or effect.tool != step.tool
        or effect.request_key != step.request_key
        or effect.state_before_hash != before
        or effect.state_after_hash != after
        or before == after
    ):
        raise ArtifactError(f"missing or inconsistent effect for {step.tool.id}")


def _check_observation(expected: JsonObject, observation: JsonObject | None) -> bool:
    return observation is not None and all(
        field in observation and canonical_json(observation[field]) == canonical_json(value)
        for field, value in expected.items()
    )


def _execute(
    store: StateStore,
    registry: ToolRegistry,
    scenario: Scenario,
    recipe: OracleRecipe,
) -> tuple[list[TraceRecord], tuple[EffectRecord, ...], JsonObject]:
    current = scenario.initial_snapshot
    trace = [
        TraceRecord(
            "environment", "snapshot_loaded", {"scenario": scenario.scenario_id}, current, current
        )
    ]
    observed_effects: list[EffectRecord] = []
    response: JsonObject = recipe.claims | {"schema_version": SCHEMA_VERSION, "status": "complete"}
    for step in recipe.steps:
        result = registry.invoke(store, step.tool, step.arguments, request_key=step.request_key)
        after = store.current_state().state_hash
        _check_step_evidence(step, result, current, after)
        if result.effect is not None:
            observed_effects.append(result.effect)
        payload = step.record() | {
            "observation": result.observation,
            "error": result.error,
            "effect_id": result.effect.effect_id if result.effect is not None else None,
        }
        trace.append(TraceRecord("environment", "tool_result", payload, current, after))
        current = after
        if result.error is not None or not _check_observation(step.expected, result.observation):
            response = {
                "schema_version": SCHEMA_VERSION,
                "status": "incomplete",
                "failed_tool": step.tool.id,
            }
            response["reason"] = (
                result.error["code"] if result.error is not None else "unexpected_observation"
            )
            break
    effects = store.effects()
    if effects != tuple(observed_effects):
        raise ArtifactError("effect ledger disagrees with the oracle trace")
    trace.append(TraceRecord("oracle", "final_response", response, current, current))
    return trace, effects, response


def run_clean_scenario(manifest: ResolvedRunManifest, root: Path, fixtures: Path) -> ScenarioRun:
    recipe = RECIPES.get(manifest.scenario)
    if recipe is None or manifest.policy != "oracle":
        raise InputError("expected a versioned M1 scenario with oracle policy")
    scenario = parse_yaml_record(fixtures / recipe.fixture, Scenario)
    if (scenario.scenario_id, scenario.version) != (
        manifest.scenario.id,
        manifest.scenario.version,
    ):
        raise InputError("bundled scenario does not match its manifest reference")
    registry = build_service_registry(fixtures)
    _preflight(scenario, recipe, registry)
    try:
        snapshot = canonical_json(
            json.loads((fixtures / "state_safety_snapshot.json").read_bytes())
        )
    except ValueError as exc:
        raise InputError("invalid bundled SQLite snapshot") from exc

    run_id = stable_id(
        "run",
        manifest.seed,
        manifest.model_dump(mode="json", exclude={"artifacts_dir"}),
        scenario.model_dump(mode="json"),
        [registry.contract(ref).model_dump(mode="json") for ref in scenario.allowed_tools],
        [step.record() for step in recipe.steps],
        recipe.claims,
    )
    run_dir = run_directory(root, manifest.artifacts_dir, run_id)
    with TemporaryDirectory() as scratch:
        store = StateStore.reset(
            Path(scratch) / "state.sqlite3", scenario, snapshot, seed=manifest.seed
        )
        records, effects, response = _execute(store, registry, scenario, recipe)
        final_hash = records[-1].state_after_hash
        final = decode_snapshot(store.resolve_snapshot(final_hash))
        state: JsonObject = final.model_dump(mode="json")
        state["row_counts"] = {table: len(rows) for table, rows in final.tables.items()}
        metrics = grade_outcome(run_id, scenario.assertions, state, response)
        records.append(
            TraceRecord(
                "evaluator", "graded", metrics.model_dump(mode="json"), final_hash, final_hash
            )
        )
        trace = build_trace(run_id, manifest.seed, records)
        state_ref = StateReference(schema_version=SCHEMA_VERSION, state_hash=final_hash)
        artifacts = {
            "resolved_config.json": canonical_json(manifest.model_dump(mode="json")) + b"\n",
            "trace.jsonl": trace,
            "metrics.json": canonical_json(metrics.model_dump(mode="json")) + b"\n",
            "state_refs.json": canonical_json(state_ref.model_dump(mode="json")) + b"\n",
            "response.json": canonical_json(response) + b"\n",
            "effects.jsonl": b"".join(
                canonical_json(effect.model_dump(mode="json")) + b"\n" for effect in effects
            ),
        }
        for state_hash in dict.fromkeys(record.state_after_hash for record in records):
            filename, payload = store.snapshot_artifact(state_hash)
            artifacts[filename] = payload
    publish_artifacts(run_dir, artifacts)
    return ScenarioRun(run_dir, metrics.safety_adjusted_success, final_hash, content_hash(trace))
