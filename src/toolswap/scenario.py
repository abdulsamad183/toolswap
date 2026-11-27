"""Run and grade the bundled read-only feasibility scenario."""

from dataclasses import dataclass
from errno import EEXIST, ENOTEMPTY
from pathlib import Path
from tempfile import TemporaryDirectory

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as SchemaValidationError
from pydantic import JsonValue

from toolswap.config import InputError, parse_yaml_record, resolve_config, scoped_path
from toolswap.contracts import (
    SCHEMA_VERSION,
    Action,
    Assertion,
    AssertionResult,
    Event,
    JsonObject,
    MetricReport,
    RecoveryDecision,
    Reference,
    Scenario,
    StateReference,
    ToolContract,
)
from toolswap.determinism import LogicalClock, canonical_json, content_hash, stable_id
from toolswap.trust import parse_proposal

FIXTURES = Path(__file__).with_name("fixtures")
CASE_ID = "mock-case-1"
CASE_READ = Reference(id="service_ops.case_read", version="1.0.0")
CASES_GET = Reference(id="cases.get", version="1.0.0")


class ArtifactError(OSError):
    """Stored artifacts disagree with a fresh deterministic run."""


@dataclass(frozen=True)
class ScenarioRun:
    run_dir: Path
    passed: bool
    state_hash: str
    trace_hash: str


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


def _grade(assertion: Assertion, state: JsonObject) -> AssertionResult:
    value: JsonValue = state
    found = True
    for part in assertion.path:
        if isinstance(part, str) and isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(part, int) and isinstance(value, list) and part < len(value):
            value = value[part]
        else:
            found = False
            break
    return AssertionResult(
        schema_version=SCHEMA_VERSION,
        assertion_id=assertion.assertion_id,
        kind=assertion.kind,
        passed=found and canonical_json(value) == canonical_json(assertion.equals),
        evidence={"found": found, "actual": value if found else None, "expected": assertion.equals},
    )


def _trace(
    run_id: str, seed: int, state_hash: str, records: list[tuple[str, str, JsonObject]]
) -> bytes:
    clock = LogicalClock()
    previous_hash = None
    rows = []
    for sequence, (actor, event_type, payload) in enumerate(records):
        fields: JsonObject = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "event_id": stable_id("event", seed, run_id, sequence),
            "sequence": sequence,
            "actor": actor,
            "event_type": event_type,
            "payload": payload,
            "logical_time": clock.tick(),
            "state_before_hash": state_hash,
            "state_after_hash": state_hash,
            "previous_event_hash": previous_hash,
        }
        previous_hash = content_hash(canonical_json(fields))
        event = Event.model_validate(fields | {"integrity_hash": previous_hash})
        rows.append(canonical_json(event.model_dump(mode="json")) + b"\n")
    return b"".join(rows)


def _verify_artifacts(folder: Path, expected: dict[str, bytes]) -> None:
    if folder.is_symlink() or not folder.is_dir():
        raise ArtifactError("run directory is invalid")
    for relative, data in expected.items():
        path = folder / relative
        if (
            path.is_symlink()
            or path.resolve() != path
            or not path.is_file()
            or path.stat().st_size != len(data)
            or path.read_bytes() != data
        ):
            raise ArtifactError(f"stored artifact differs from replay: {relative}")


def _publish(folder: Path, expected: dict[str, bytes]) -> None:
    if folder.exists() or folder.is_symlink():
        _verify_artifacts(folder, expected)
        return
    folder.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=folder.parent) as scratch:
        staging = Path(scratch) / "bundle"
        for relative, data in expected.items():
            path = staging / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        try:
            staging.rename(folder)
        except OSError as exc:
            if exc.errno not in {EEXIST, ENOTEMPTY}:
                raise
            _verify_artifacts(folder, expected)


def run_scenario(path: Path, root: Path) -> ScenarioRun:
    manifest = resolve_config(path, root)
    if manifest.scenario != CASE_READ or manifest.policy != "stub":
        raise InputError("feasibility requires service_ops.case_read@1.0.0 with stub policy")
    scenario = parse_yaml_record(FIXTURES / "case_read.yaml", Scenario)
    contract = parse_yaml_record(FIXTURES / "cases_get.yaml", ToolContract)
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
    results = tuple(_grade(assertion, state) for assertion in scenario.assertions)
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
    trace = _trace(
        run_id,
        manifest.seed,
        state_hash,
        [
            ("environment", "snapshot_loaded", {"scenario": scenario.scenario_id}),
            ("environment", "tool_result", {"tool": contract.tool_id, "observation": observation}),
            ("policy", "recovery_proposal", decision.model_dump(mode="json")),
            ("evaluator", "graded", metrics.model_dump(mode="json")),
        ],
    )
    _, artifact_root = scoped_path(root, manifest.artifacts_dir)
    if (artifact_root / "runs").is_symlink():
        raise ArtifactError("run storage must be a local directory")
    run_dir = artifact_root / "runs" / run_id
    state_ref = StateReference(schema_version=SCHEMA_VERSION, state_hash=state_hash)
    _publish(
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
