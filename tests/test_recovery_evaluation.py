from pathlib import Path

import pytest

from toolswap.contracts import EffectRecord, Event, MetricReport
from toolswap.recovery.controller import Controller
from toolswap.recovery.evaluation import grade_recovery
from toolswap.recovery.records import Termination
from toolswap.recovery.scenarios import PREVIEWS, _scenario
from toolswap.scenario import FIXTURES, run_scenario
from toolswap.service_contracts import tool_reference
from toolswap.state_snapshots import decode_snapshot

EXAMPLES = Path("examples/recovery").resolve()


@pytest.mark.parametrize("cause", ["state", "communication"])
def test_claims_cannot_override_assertions(tmp_path, monkeypatch, cause):
    if cause == "state":
        manifest = "unresolved-timeout.yaml"
        run = Controller.run

        def claim_success(controller, steps, claims, **kwargs):
            return run(controller, steps, claims, **kwargs) | claims | {"status": "complete"}

        monkeypatch.setattr(Controller, "run", claim_success)
    else:
        manifest = "schema-repair.yaml"
        ref = tool_reference("service_ops.schema_repair")
        preview = PREVIEWS[ref]
        monkeypatch.setitem(
            PREVIEWS,
            ref,
            preview.model_copy(update={"claims": preview.claims | {"notes": "wrong"}}),
        )
    result = run_scenario(EXAMPLES / manifest, tmp_path)
    assert not result.passed
    metrics = MetricReport.model_validate_json((result.run_dir / "metrics.json").read_bytes())
    assert metrics.false_success and not metrics.task_success
    assert not metrics.safety_adjusted_success
    assert any(
        not assertion.passed
        and assertion.kind == ("required" if cause == "state" else "communication")
        for assertion in metrics.assertions
    )


def test_evaluator_checks_verified_steps_and_execution_evidence(tmp_path):
    run = run_scenario(EXAMPLES / "committed-timeout.yaml", tmp_path)
    folder = run.run_dir
    events = [
        Event.model_validate_json(row) for row in (folder / "trace.jsonl").read_bytes().splitlines()
    ]
    terminal = Termination.model_validate_json((folder / "termination.json").read_bytes())
    state = decode_snapshot(
        (folder / "states" / f"{run.state_hash.removeprefix('sha256:')}.json").read_bytes()
    )
    document = state.model_dump(mode="json")
    document["row_counts"] = {table: len(rows) for table, rows in state.tables.items()}
    effects = [
        EffectRecord.model_validate_json(row)
        for row in (folder / "effects.jsonl").read_bytes().splitlines()
    ]
    ref = tool_reference("service_ops.committed_timeout")
    preview = PREVIEWS[ref]
    response = events[-3].payload
    scenario = _scenario(ref, preview, FIXTURES)
    count = len(preview.steps)

    missing_step = [
        e for e in events if not (e.event_type == "step_completed" and e.payload["step"] == 0)
    ]
    unverified = grade_recovery(
        events[0].run_id, scenario, document, response, terminal, missing_step, effects, count
    )
    assert all(a.passed for a in unverified.assertions)
    assert not unverified.task_success and unverified.false_success

    extra_dispatch = [
        e
        for e in events
        if not (e.event_type == "initial_authorization" and e.payload["step"] == 0)
    ]
    unsafe = grade_recovery(
        events[0].run_id, scenario, document, response, terminal, extra_dispatch, effects, count
    )
    assert unsafe.unsafe_executions == 1 and not unsafe.safety_adjusted_success

    repeat = effects[0].model_copy(
        update={
            "effect_id": "extra",
            "status": "duplicate",
            "state_before_hash": effects[0].state_after_hash,
        }
    )
    new_event = events[0].model_copy(
        update={"event_type": "effect_recorded", "payload": repeat.model_dump(mode="json")}
    )
    repeat_report = grade_recovery(
        events[0].run_id,
        scenario,
        document,
        response,
        terminal,
        [*events, new_event],
        [*effects, repeat],
        count,
    )
    assert repeat_report.duplicate_effects == 0
    duplicate = repeat.model_copy(update={"status": "complete"})
    duplicate_event = new_event.model_copy(update={"payload": duplicate.model_dump(mode="json")})
    duplicate_report = grade_recovery(
        events[0].run_id,
        scenario,
        document,
        response,
        terminal,
        [*events, duplicate_event],
        [*effects, duplicate],
        count,
    )
    assert duplicate_report.duplicate_effects == 1 and not duplicate_report.safety_adjusted_success
