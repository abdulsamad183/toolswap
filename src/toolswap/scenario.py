"""Dispatch bundled scenarios and preserve the original feasibility replay."""

from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as SchemaValidationError

from toolswap.config import InputError, parse_yaml_record, resolve_config
from toolswap.contracts import (
    SCHEMA_VERSION,
    Action,
    JsonObject,
    MetricReport,
    RecoveryDecision,
    Reference,
    ResolvedRunManifest,
    Scenario,
    StateReference,
    ToolContract,
)
from toolswap.determinism import canonical_json, content_hash, stable_id
from toolswap.grading import evaluate_assertion
from toolswap.m1 import run_clean_scenario
from toolswap.replay import (
    ArtifactError,
    ScenarioRun,
    TraceRecord,
    build_trace,
    publish_artifacts,
    run_directory,
)
from toolswap.service_operations import build_service_registry
from toolswap.trust import parse_proposal

FIXTURES = Path(__file__).with_name("fixtures")
CASE_ID = "mock-case-1"
CASE_READ = Reference(id="service_ops.case_read", version="1.0.0")
CASES_GET = Reference(id="cases.get", version="1.0.0")


def _read_case(state: JsonObject, contract: ToolContract) -> JsonObject:
    arguments = {"case_id": CASE_ID}
    cases = state.get("cases")
    case = cases.get(CASE_ID) if isinstance(cases, dict) else None
    if not isinstance(case, dict):
        raise InputError("fixture case is missing from its snapshot")
    observation = dict(case)
    try:
        Draft202012Validator(contract.input_schema).validate(arguments)
        Draft202012Validator(contract.output_schema).validate(observation)
    except SchemaValidationError as exc:
        raise InputError("fixture case read does not match its tool contract") from exc
    return observation


def _stub_proposal(observation: JsonObject) -> bytes:
    decision = RecoveryDecision(
        schema_version=SCHEMA_VERSION,
        verdict="RESULT_ACCEPTABLE",
        failure_family=None,
        confidence=1.0,
        evidence=(f"The tool returned case {observation['case_id']}.",),
        action=Action.ACCEPT,
        tool=None,
        arguments={},
        expected_postcondition=None,
        requires_confirmation=False,
    )
    return canonical_json(decision.model_dump(mode="json"))


def run_scenario(path: Path, root: Path) -> ScenarioRun:
    manifest = resolve_config(path, root)
    if manifest.scenario == CASE_READ:
        return _run_feasibility(manifest, root)
    return run_clean_scenario(manifest, root, FIXTURES)


def _run_feasibility(manifest: ResolvedRunManifest, root: Path) -> ScenarioRun:
    if manifest.policy != "stub":
        raise InputError("feasibility requires service_ops.case_read@1.0.0 with stub policy")
    scenario = parse_yaml_record(FIXTURES / "case_read.yaml", Scenario)
    registry = build_service_registry(FIXTURES)
    registry.audit_scenario(scenario)
    contract = registry.contract(CASES_GET)
    if (
        (scenario.scenario_id, scenario.version) != (CASE_READ.id, CASE_READ.version)
        or scenario.allowed_tools != (CASES_GET,)
        or (contract.tool_id, contract.version) != (CASES_GET.id, CASES_GET.version)
        or contract.side_effect != "none"
        or contract.requires_confirmation
        or any(assertion.kind != "required" for assertion in scenario.assertions)
    ):
        raise InputError("unsupported fixture or tool contract for the read-only runner")

    state: JsonObject = {"cases": {CASE_ID: {"case_id": CASE_ID, "status": "open"}}}
    snapshot = canonical_json(state)
    state_hash = content_hash(snapshot)
    if scenario.initial_snapshot != state_hash:
        raise InputError("fixture initial state hash does not match its declared snapshot")
    observation = _read_case(state, contract)
    if canonical_json(state) != snapshot:
        raise ArtifactError("read-only call changed the mock state")
    decision = parse_proposal(_stub_proposal(observation), {CASES_GET.id})
    results = tuple(
        evaluate_assertion(assertion, state, include_path=False)
        for assertion in scenario.assertions
    )
    passed = all(result.passed for result in results)

    run_id = stable_id(
        "run",
        manifest.seed,
        manifest.model_dump(mode="json", exclude={"artifacts_dir"}),
        scenario.model_dump(mode="json"),
        contract.model_dump(mode="json"),
    )
    metrics = MetricReport(
        schema_version=SCHEMA_VERSION,
        run_id=run_id,
        task_success=passed,
        safety_adjusted_success=passed,
        unsafe_proposals=0,
        unsafe_executions=0,
        duplicate_effects=0,
        false_success=False,
        false_interventions=0,
        assertions=results,
    )
    records: list[tuple[str, str, JsonObject]] = [
        ("environment", "snapshot_loaded", {"scenario": scenario.scenario_id}),
        ("environment", "tool_result", {"tool": contract.tool_id, "observation": observation}),
        ("policy", "recovery_proposal", decision.model_dump(mode="json")),
        ("evaluator", "graded", metrics.model_dump(mode="json")),
    ]
    trace = build_trace(
        run_id,
        manifest.seed,
        [
            TraceRecord(actor, event, payload, state_hash, state_hash)
            for actor, event, payload in records
        ],
    )
    run_dir = run_directory(root, manifest.artifacts_dir, run_id)
    state_ref = StateReference(schema_version=SCHEMA_VERSION, state_hash=state_hash)
    publish_artifacts(
        run_dir,
        {
            "resolved_config.json": canonical_json(manifest.model_dump(mode="json")) + b"\n",
            "trace.jsonl": trace,
            "metrics.json": canonical_json(metrics.model_dump(mode="json")) + b"\n",
            "state_refs.json": canonical_json(state_ref.model_dump(mode="json")) + b"\n",
            f"states/{state_hash.removeprefix('sha256:')}.json": snapshot,
        },
    )
    return ScenarioRun(run_dir, passed, state_hash, content_hash(trace))
