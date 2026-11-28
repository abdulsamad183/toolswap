import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from toolswap.cli import app
from toolswap.contracts import Action, EffectRecord, Event, MetricReport, Reference
from toolswap.determinism import canonical_json, content_hash
from toolswap.recovery.evidence import verify_bundle
from toolswap.recovery.records import Termination
from toolswap.recovery.scenarios import PREVIEWS, _proposal
from toolswap.scenario import run_scenario
from toolswap.service_contracts import tool_reference
from toolswap.service_tools import ToolRegistry
from toolswap.state import StateStore

MANIFEST = Path("examples/recovery/committed-timeout.yaml").resolve()


def _reseal(bundle, relative, data, **seal_changes):
    (bundle / relative).write_bytes(data)
    path = bundle / "integrity.json"
    seal = json.loads(path.read_bytes())
    seal["files"][relative] = content_hash(data)
    path.write_bytes(canonical_json(seal | seal_changes) + b"\n")


def _verify(path, *options):
    return CliRunner().invoke(app, ["trace", "verify", "--run-dir", str(path), *options])


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("payload", "broken event chain"),
        ("reorder", "broken event chain"),
        ("duplicate", "broken event chain"),
        ("truncated", "event count differs"),
        ("resealed_tail", "terminal, response, or evaluator evidence"),
        ("missing_state", "missing or unsafe artifact"),
        ("changed_state", "state differs"),
        ("symlink", "missing or unsafe artifact"),
        ("ledger", "ledger and write snapshots differ"),
        ("response", "terminal, response, or evaluator evidence"),
        ("manifest", "resolved manifest differs from the trace"),
        ("metrics", "terminal, response, or evaluator evidence"),
        ("foreign_file", "unexpected artifact inventory"),
    ],
)
def test_offline_verify_rejects_tampered_bundles(tmp_path, change, reason):
    bundle = run_scenario(MANIFEST, tmp_path).run_dir
    trace = (bundle / "trace.jsonl").read_bytes().splitlines(keepends=True)
    snapshot = next((bundle / "states").iterdir())
    snapshot_name = snapshot.relative_to(bundle).as_posix()
    if change == "payload":
        event = json.loads(trace[2])
        event["payload"]["task"] = "A different task."
        trace[2] = canonical_json(event) + b"\n"
        _reseal(bundle, "trace.jsonl", b"".join(trace))
    elif change == "reorder":
        trace[1], trace[2] = trace[2], trace[1]
        _reseal(bundle, "trace.jsonl", b"".join(trace))
    elif change == "duplicate":
        trace[2] = trace[1]
        _reseal(bundle, "trace.jsonl", b"".join(trace))
    elif change == "truncated":
        _reseal(bundle, "trace.jsonl", b"".join(trace[:-1]))
    elif change == "resealed_tail":
        _reseal(
            bundle,
            "trace.jsonl",
            b"".join(trace[:-1]),
            event_count=len(trace) - 1,
            final_event_hash=json.loads(trace[-2])["integrity_hash"],
        )
    elif change == "missing_state":
        snapshot.unlink()
    elif change == "changed_state":
        _reseal(bundle, snapshot_name, snapshot.read_bytes() + b" ")
    elif change == "symlink":
        moved = tmp_path / snapshot.name
        snapshot.rename(moved)
        snapshot.symlink_to(moved)
    elif change == "ledger":
        rows = (bundle / "effects.jsonl").read_bytes().splitlines(keepends=True)
        first = json.loads(rows[0])
        first["state_after_hash"] = first["state_before_hash"]
        rows[0] = canonical_json(first) + b"\n"
        _reseal(bundle, "effects.jsonl", b"".join(rows))
    elif change == "response":
        _reseal(bundle, "response.json", b'{"status":"changed"}\n')
    elif change == "manifest":
        manifest = json.loads((bundle / "resolved_config.json").read_bytes())
        _reseal(
            bundle,
            "resolved_config.json",
            canonical_json(manifest | {"artifacts_dir": "other"}) + b"\n",
        )
    elif change == "metrics":
        metrics = json.loads((bundle / "metrics.json").read_bytes())
        _reseal(bundle, "metrics.json", canonical_json(metrics | {"task_success": False}) + b"\n")
    else:
        (bundle / "unexpected.json").write_bytes(b"{}\n")

    result = _verify(bundle)
    assert result.exit_code == 3 and reason in result.stderr


def test_offline_verify_uses_expected_digest_without_sqlite(tmp_path, monkeypatch):
    result = run_scenario(MANIFEST.with_name("unresolved-timeout.yaml"), tmp_path)
    assert not result.passed
    before = {p: p.read_bytes() for p in result.run_dir.rglob("*") if p.is_file()}
    monkeypatch.setattr(StateStore, "_open", lambda *a, **k: pytest.fail("opened SQLite"))
    success = _verify(result.run_dir, "--expected-trace-hash", result.trace_hash)
    assert success.exit_code == 0 and "Integrity: PASS" in success.stdout
    mismatch = _verify(result.run_dir, "--expected-trace-hash", "sha256:" + "0" * 64)
    assert mismatch.exit_code == 3 and "trace differs from expected hash" in mismatch.stderr
    assert _verify(result.run_dir, "--expected-trace-hash", "bad").exit_code == 2
    assert _verify(tmp_path / "missing").exit_code == 3
    assert before == {p: p.read_bytes() for p in result.run_dir.rglob("*") if p.is_file()}


@pytest.mark.parametrize(
    ("action", "kind"),
    [
        (Action.STOP, "policy_stop"),
        (Action.REQUEST_INFORMATION, "information_required"),
        (Action.REQUIRE_CONFIRMATION, "confirmation_required"),
    ],
)
def test_policy_handoffs_preserve_the_unresolved_step(tmp_path, monkeypatch, action, kind):
    reference = tool_reference("service_ops.committed_timeout")
    preview = PREVIEWS[reference]
    tool = preview.steps[2].tool
    scope = preview.scope.model_copy(update={"confirmation_tools": (tool,)})
    monkeypatch.setitem(
        PREVIEWS,
        reference,
        preview.model_copy(update={"script": (_proposal(action),), "scope": scope}),
    )
    result = run_scenario(MANIFEST, tmp_path)
    assert not result.passed and not result.infrastructure_failure
    assert verify_bundle(result.run_dir) == result.trace_hash
    terminal = Termination.model_validate_json((result.run_dir / "termination.json").read_bytes())
    assert terminal.kind == kind and terminal.unresolved_step == 2 and terminal.tool == tool
    ledger = [
        EffectRecord.model_validate_json(line)
        for line in (result.run_dir / "effects.jsonl").read_bytes().splitlines()
    ]
    assert len(ledger) == 1 and ledger[0].tool == tool


@pytest.mark.parametrize("after_commit", [False, True])
def test_infrastructure_failure_seals_last_evidence(tmp_path, monkeypatch, after_commit):
    invoke = ToolRegistry.invoke

    def fail(registry, store, tool: Reference, arguments, **keywords):
        if tool.id == "work_orders.create":
            if after_commit:
                invoke(registry, store, tool, arguments, **keywords)
            raise sqlite3.OperationalError("machine-specific text")
        return invoke(registry, store, tool, arguments, **keywords)

    monkeypatch.setattr(ToolRegistry, "invoke", fail)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["scenario", "run", "--manifest", str(MANIFEST)])
    assert result.exit_code == 3 and "Outcome: ERROR" in result.stdout
    bundle = next(Path("artifacts/runs").iterdir())
    assert verify_bundle(bundle) in result.stdout
    terminal = Termination.model_validate_json((bundle / "termination.json").read_bytes())
    assert terminal.kind == "infrastructure_failure" and terminal.reason == "sqlite_failure"
    assert terminal.unresolved_step == 2 and terminal.tool.id == "work_orders.create"
    trace = (bundle / "trace.jsonl").read_bytes()
    assert b"machine-specific text" not in trace
    events = [Event.model_validate_json(line) for line in trace.splitlines()]
    assert [e.event_type for e in events if e.event_type == "terminated"] == ["terminated"]
    assert events[-1].payload == terminal.model_dump(mode="json")
    assert {e.payload["call"]["tool"]["id"] for e in events if e.event_type == "tool_dispatch"} == {
        "cases.get",
        "inventory.get_availability",
        "work_orders.create",
    }
    effects = [
        EffectRecord.model_validate_json(line)
        for line in (bundle / "effects.jsonl").read_bytes().splitlines()
    ]
    if after_commit:
        assert len(effects) == 1 and effects[0].status == "complete"
        assert effects[0].state_after_hash == terminal.state_hash
    else:
        assert not effects and terminal.state_hash == events[0].state_before_hash
    metrics = MetricReport.model_validate_json((bundle / "metrics.json").read_bytes())
    assert not metrics.task_success and not metrics.false_success and metrics.unsafe_executions == 0
    monkeypatch.setattr(ToolRegistry, "invoke", invoke)
    retry = run_scenario(MANIFEST, tmp_path)
    assert retry.passed and retry.run_dir.relative_to(tmp_path) != bundle
    assert verify_bundle(bundle) in result.stdout
