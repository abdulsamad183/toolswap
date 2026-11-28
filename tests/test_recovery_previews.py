import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from toolswap.cli import app
from toolswap.contracts import EffectRecord, Event, MetricReport, Reference
from toolswap.recovery.records import Fault, Observation
from toolswap.recovery.scenarios import PREVIEWS
from toolswap.scenario import run_scenario
from toolswap.state import StateStore

EXAMPLES = Path("examples/recovery").resolve()
INITIAL = "sha256:010afeb5899c67b445b21b37d8c1c5aa7760c7cd6b384e8412dd4978a36213ad"


def _read_bundle(path: Path):
    trace = [
        Event.model_validate_json(row) for row in (path / "trace.jsonl").read_bytes().splitlines()
    ]
    effects = [
        EffectRecord.model_validate_json(row)
        for row in (path / "effects.jsonl").read_bytes().splitlines()
    ]
    final = trace[-1].state_after_hash
    assert final is not None
    state = json.loads((path / "states" / f"{final.removeprefix('sha256:')}.json").read_bytes())[
        "tables"
    ]
    reply = json.loads((path / "response.json").read_bytes())
    return trace, effects, state, reply


def _calls(events):
    return [
        Observation.model_validate(event.payload["visible"])
        for event in events
        if event.event_type == "tool_result"
    ]


@pytest.mark.parametrize(
    ("name", "exit_code"),
    [
        ("committed-timeout", 0),
        ("unresolved-timeout", 1),
        ("schema-repair", 0),
        ("unknown-case-repair", 1),
    ],
)
def test_preview_cli_replays_deterministically(tmp_path, monkeypatch, name, exit_code):
    manifest = EXAMPLES / f"{name}.yaml"
    monkeypatch.chdir(tmp_path)
    args = ["scenario", "run", "--manifest", str(manifest)]
    first = CliRunner().invoke(app, args)
    second = CliRunner().invoke(app, args)
    assert first.exit_code == second.exit_code == exit_code, first.output
    assert first.stdout == second.stdout
    assert ("Outcome: PASS" if exit_code == 0 else "Outcome: FAIL") in first.stdout
    path = next(Path("artifacts/runs").iterdir())
    result = run_scenario(manifest, tmp_path / "independent")
    assert result.passed is (exit_code == 0)
    local = {f.relative_to(path): f.read_bytes() for f in path.rglob("*") if f.is_file()}
    independent = {
        f.relative_to(result.run_dir): f.read_bytes()
        for f in result.run_dir.rglob("*")
        if f.is_file()
    }
    assert independent == local


def test_m2_verifies_commit_before_accept_and_never_retries_create(tmp_path):
    result = run_scenario(EXAMPLES / "committed-timeout.yaml", tmp_path)
    events, effects, state, reply = _read_bundle(result.run_dir)
    calls = _calls(events)
    assert [call.call.tool.id for call in calls] == [
        "cases.get",
        "inventory.get_availability",
        "work_orders.create",
        "work_orders.get_by_idempotency_key",
        "inventory.reserve",
        "notifications.send",
    ]
    assert calls[2].error["code"] == "timeout"
    assert calls[3].call.arguments == {"idempotency_key": calls[2].call.request_key}
    assert calls[3].result == effects[0].result
    assert [effect.status for effect in effects] == ["complete"] * 3
    assert len(state["work_orders"]) == len(state["notifications"]) == 1
    assert state["inventory"][0]["reserved_count"] == state["inventory"][0]["available_count"] == 1
    assert reply["status"] == "complete"
    decisions = [
        event.payload["action"] for event in events if event.event_type == "recovery_proposal"
    ]
    assert decisions == ["VERIFY", "ACCEPT"]
    accepted = next(
        event.sequence for event in events if event.payload.get("rule_id") == "verified_accept"
    )
    verified = next(
        event for event in events if event.payload.get("probe_call_id") == calls[3].call.call_id
    )
    assert verified.payload["status"] == "verified" and verified.sequence < accepted
    first_input = next(event.payload for event in events if event.event_type == "policy_input")
    assert first_input["observation"] == calls[2].model_dump(mode="json")
    assert first_input["observation"]["result"] is None
    assert "effect_id" not in json.dumps(first_input) and "elapsed_ns" not in json.dumps(
        first_input
    )
    assert (
        result.state_hash
        == "sha256:45553771dd751776c18dbc8fdebe512ec8bd576a38666193433cb028e7e17e39"
    )


def test_m2_inconclusive_lookup_preserves_the_order_and_stops(tmp_path):
    result = run_scenario(EXAMPLES / "unresolved-timeout.yaml", tmp_path)
    events, effects, state, reply = _read_bundle(result.run_dir)
    assert not result.passed and reply["reason"] == "verification_inconclusive"
    assert [entry.tool.id for entry in effects] == ["work_orders.create"]
    assert len(state["work_orders"]) == 1 and not state["notifications"]
    assert state["inventory"][0]["reserved_count"] == 0
    assert [call.call.tool.id for call in _calls(events)][-2:] == [
        "work_orders.create",
        "work_orders.get_by_idempotency_key",
    ]


@pytest.mark.parametrize("invented", [False, True])
def test_m3_repair_uses_trusted_notes_or_blocks_invented_identifier(tmp_path, invented):
    filename = "unknown-case-repair.yaml" if invented else "schema-repair.yaml"
    result = run_scenario(EXAMPLES / filename, tmp_path)
    events, effects, state, reply = _read_bundle(result.run_dir)
    calls = _calls(events)
    assert calls[1].call.tool.version == "2.0.0"
    assert calls[1].error["code"] == "invalid_arguments" and "notes" in calls[1].call.arguments
    rejected = [event for event in events if event.event_type == "tool_result"][1]
    assert rejected.payload["entered_environment"] is False
    assert rejected.state_before_hash == rejected.state_after_hash == INITIAL
    if invented:
        assert not result.passed and not effects and len(calls) == 2
        assert reply["reason"] == "argument_without_provenance" and result.state_hash == INITIAL
        assert (
            next(
                e.payload["evidence"]["field"]
                for e in events
                if e.payload.get("rule_id") == "argument_without_provenance"
            )
            == "case_id"
        )
    else:
        assert result.passed and len(effects) == 1 and effects[0].status == "complete"
        assert calls[2].call.request_key == calls[1].call.request_key
        assert "new_notes" in calls[2].call.arguments and "notes" not in calls[2].call.arguments
        assert calls[3].call.tool.id == "cases.get"
        assert calls[3].result["notes"] == state["cases"][0]["notes"] == reply["notes"]
        repair = next(
            e.payload["evidence"]
            for e in events
            if e.payload.get("rule_id") == "authorized_dispatch"
        )
        assert repair["provenance"]["expected_notes"]["call_id"] == calls[0].call.call_id
        assert repair["provenance"]["new_notes"] == {"source": "task", "field": "notes"}
    metrics = MetricReport.model_validate_json((result.run_dir / "metrics.json").read_bytes())
    assert metrics.task_success is not invented and not metrics.false_success


def test_invalid_fault_is_rejected_before_reset(tmp_path, monkeypatch):
    ref = Reference(id="service_ops.committed_timeout", version="1.0.0")
    corrupt = PREVIEWS[ref].model_copy(update={"faults": (Fault(step=99, point="initial_after"),)})
    monkeypatch.setitem(PREVIEWS, ref, corrupt)
    monkeypatch.setattr(
        StateStore, "reset", lambda *a, **k: pytest.fail("opened SQLite before preflight")
    )
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        app, ["scenario", "run", "--manifest", str(EXAMPLES / "committed-timeout.yaml")]
    )
    assert result.exit_code == 2 and "unknown step" in result.stderr
    assert not Path("artifacts").exists()
