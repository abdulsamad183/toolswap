import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from toolswap import m1, scenario
from toolswap.cli import app
from toolswap.contracts import EffectRecord, Event, MetricReport
from toolswap.determinism import canonical_json, content_hash, stable_id
from toolswap.service_contracts import tool_reference
from toolswap.service_tools import ToolResult

EXAMPLES = Path("examples/m1").resolve()
REPLACEMENT = EXAMPLES / "replacement.yaml"
INITIAL = "sha256:010afeb5899c67b445b21b37d8c1c5aa7760c7cd6b384e8412dd4978a36213ad"
REPLACEMENT_STATE = "sha256:45553771dd751776c18dbc8fdebe512ec8bd576a38666193433cb028e7e17e39"
REPLACEMENT_TRACE = "sha256:a0918ac13b798e8fd73b3d97f80855ce82581d374db021529cb33b5fc74d10b9"
INSPECTION_TRACE = "sha256:cf7fb5b904879a14fea073b7ca306f19b70f0007d13c490f5b64613eaaf04369"


def _cli(manifest: Path = REPLACEMENT):
    return CliRunner().invoke(app, ["scenario", "run", "--manifest", str(manifest)])


def _bundle(path: Path) -> dict[str, bytes]:
    return {
        file.relative_to(path).as_posix(): file.read_bytes()
        for file in path.rglob("*")
        if file.is_file()
    }


def _snapshot(bundle: dict[str, bytes], state_hash: str):
    return json.loads(bundle[f"states/{state_hash.removeprefix('sha256:')}.json"])


@pytest.mark.parametrize(
    ("name", "state_hash", "trace_hash", "calls", "writes"),
    [
        ("inspection", INITIAL, INSPECTION_TRACE, ("cases.get", "accounts.verify"), ()),
        (
            "replacement",
            REPLACEMENT_STATE,
            REPLACEMENT_TRACE,
            (
                "cases.get",
                "inventory.get_availability",
                "work_orders.create",
                "inventory.reserve",
                "notifications.send",
            ),
            ("replacement-order", "replacement-stock", "replacement-email"),
        ),
    ],
)
def test_clean_scenario_golden_replay(
    tmp_path: Path, name, state_hash, trace_hash, calls, writes
) -> None:
    manifest = EXAMPLES / f"{name}.yaml"
    first = scenario.run_scenario(manifest, tmp_path / "first")
    again = scenario.run_scenario(manifest, tmp_path / "second")
    assert first.passed and again.passed
    assert first.run_dir.name == again.run_dir.name
    assert first.state_hash == again.state_hash == state_hash
    assert first.trace_hash == again.trace_hash == trace_hash
    artifacts = _bundle(first.run_dir)
    assert artifacts == _bundle(again.run_dir)
    assert scenario.run_scenario(manifest, tmp_path / "first") == first

    events = [Event.model_validate_json(row) for row in artifacts["trace.jsonl"].splitlines()]
    ledger = [
        EffectRecord.model_validate_json(row) for row in artifacts["effects.jsonl"].splitlines()
    ]
    metrics = MetricReport.model_validate_json(artifacts["metrics.json"])
    assert [event.event_type for event in events] == [
        "snapshot_loaded",
        *(["tool_result"] * len(calls)),
        "final_response",
        "graded",
    ]
    assert [event.payload["tool"]["id"] for event in events[1:-2]] == list(calls)
    assert [event.payload["effect_id"] for event in events[1:-2] if event.payload["effect_id"]] == [
        record.effect_id for record in ledger
    ]
    assert [record.request_key for record in ledger] == list(writes)
    assert all(record.status == "complete" for record in ledger)
    assert metrics.task_success and metrics.safety_adjusted_success and not metrics.false_success
    assert all(result.passed for result in metrics.assertions)
    assert events[-1].payload == metrics.model_dump(mode="json")
    assert events[-2].payload == json.loads(artifacts["response.json"])
    assert json.loads(artifacts["state_refs.json"])["state_hash"] == state_hash
    assert content_hash(artifacts["trace.jsonl"]) == trace_hash
    assert len([file for file in artifacts if file.startswith("states/")]) == len(writes) + 1
    previous_hash = None
    current_state = INITIAL
    for sequence, event in enumerate(events):
        assert event.event_id == stable_id("event", 0, event.run_id, sequence)
        assert event.sequence == event.logical_time == sequence
        assert event.previous_event_hash == previous_hash
        assert event.integrity_hash == content_hash(
            canonical_json(event.model_dump(mode="json", exclude={"integrity_hash"}))
        )
        assert event.state_before_hash == current_state
        current_state = event.state_after_hash
        assert current_state is not None
        assert (
            content_hash(artifacts[f"states/{current_state.removeprefix('sha256:')}.json"])
            == current_state
        )
        previous_hash = event.integrity_hash
    assert current_state == state_hash
    for record, event in zip(
        ledger, [e for e in events if e.payload.get("effect_id")], strict=True
    ):
        assert record.state_before_hash == event.state_before_hash
        assert record.state_after_hash == event.state_after_hash

    expected = json.loads((scenario.FIXTURES / "state_safety_snapshot.json").read_bytes())
    if name == "replacement":
        expected["tables"]["work_orders"] = [
            {
                "work_order_id": stable_id("work_order", 0, "replacement-order"),
                "case_id": "mock-case-1",
                "action": "replace",
                "status": "created",
                "request_key": "replacement-order",
            }
        ]
        expected["tables"]["inventory"][0].update(available_count=1, reserved_count=1)
        expected["tables"]["notifications"] = [
            {
                "message_id": stable_id("notification", 0, "replacement-email"),
                "case_id": "mock-case-1",
                "channel": "email",
                "status": "delivered",
                "request_key": "replacement-email",
            }
        ]
    assert _snapshot(artifacts, state_hash) == expected


def test_cli_stops_on_tool_error_and_grades_partial_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = m1.build_service_registry(scenario.FIXTURES)
    invoke = registry.invoke

    def fail_notification(store, ref, arguments, *, request_key=None):
        if ref.id == "notifications.send":
            return ToolResult(
                error={"code": "not_found", "message": "Notification channel unavailable"}
            )
        return invoke(store, ref, arguments, request_key=request_key)

    monkeypatch.setattr(registry, "invoke", fail_notification)
    monkeypatch.setattr(m1, "build_service_registry", lambda _: registry)
    monkeypatch.chdir(tmp_path)
    result = _cli()
    assert result.exit_code == 1 and "Outcome: FAIL" in result.stdout
    artifacts = _bundle(next((tmp_path / "artifacts" / "runs").iterdir()))
    assert json.loads(artifacts["response.json"]) == {
        "schema_version": "1.0.0",
        "status": "incomplete",
        "failed_tool": "notifications.send",
        "reason": "not_found",
    }
    assert len(artifacts["effects.jsonl"].splitlines()) == 2
    metrics = MetricReport.model_validate_json(artifacts["metrics.json"])
    assert not metrics.task_success and not metrics.false_success
    assert not next(
        r.passed for r in metrics.assertions if r.assertion_id == "delivered_notification"
    )
    assert (
        json.loads(artifacts["trace.jsonl"].splitlines()[-3])["payload"]["error"]["code"]
        == "not_found"
    )


@pytest.mark.parametrize("invalid", ["version", "arguments", "missing_key", "repeated_key"])
def test_preflight_rejects_invalid_steps_before_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid: str
) -> None:
    reference = tool_reference("service_ops.replacement")
    recipe = m1.RECIPES[reference]
    first, second, create, reserve, send = recipe.steps
    if invalid == "version":
        create = replace(create, tool=tool_reference(create.tool.id, "2.0.0"))
    elif invalid == "arguments":
        reserve = replace(reserve, arguments=reserve.arguments | {"quantity": -1})
    elif invalid == "missing_key":
        create = replace(create, request_key=None)
    else:
        reserve = replace(reserve, request_key=create.request_key)
    monkeypatch.setitem(
        m1.RECIPES, reference, replace(recipe, steps=(first, second, create, reserve, send))
    )
    monkeypatch.setattr(
        m1.StateStore, "reset", lambda *a, **kw: pytest.fail("database opened during preflight")
    )
    monkeypatch.chdir(tmp_path)
    assert _cli().exit_code == 2
    assert not (tmp_path / "artifacts").exists()


def test_cli_rejects_tampering_without_overwriting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert _cli().exit_code == 0
    run = next((tmp_path / "artifacts" / "runs").iterdir())
    snapshot = run / "states" / f"{REPLACEMENT_STATE.removeprefix('sha256:')}.json"
    original = snapshot.read_bytes()
    snapshot.write_bytes(b"invalid")
    result = _cli()
    assert result.exit_code == 3 and "stored artifact differs from replay" in result.stderr
    assert snapshot.read_bytes() == b"invalid"
    snapshot.write_bytes(original)
    extra = run / "unexpected.json"
    extra.write_bytes(b"{}")
    result = _cli()
    assert result.exit_code == 3 and "unexpected stored artifact" in result.stderr
    assert extra.read_bytes() == b"{}"


def test_sqlite_infrastructure_error_has_exit_three(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_reset(*args, **kwargs):
        raise sqlite3.OperationalError("cannot open database")

    monkeypatch.setattr(m1.StateStore, "reset", fail_reset)
    monkeypatch.chdir(tmp_path)
    result = _cli()
    assert result.exit_code == 3 and "SQLite infrastructure failure" in result.stderr
    assert not (tmp_path / "artifacts").exists()
