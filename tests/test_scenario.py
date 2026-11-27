import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from toolswap import scenario
from toolswap.cli import app
from toolswap.config import parse_yaml_record
from toolswap.contracts import Event, ToolContract
from toolswap.determinism import canonical_json, content_hash

MANIFEST = Path("examples/feasibility/run.yaml").resolve()


def _run(path: Path = MANIFEST):
    return CliRunner().invoke(app, ["scenario", "run", "--manifest", str(path)])


def test_two_independent_runs_have_identical_artifacts(tmp_path: Path) -> None:
    roots = [tmp_path / "first", tmp_path / "second"]
    results = [scenario.run_scenario(MANIFEST, root) for root in roots]
    first, second = results
    assert first.passed and second.passed
    assert first.state_hash == second.state_hash
    assert first.trace_hash == second.trace_hash
    for path in first.run_dir.rglob("*"):
        if path.is_file():
            assert (
                path.read_bytes() == (second.run_dir / path.relative_to(first.run_dir)).read_bytes()
            )

    trace = (first.run_dir / "trace.jsonl").read_bytes()
    snapshots = list((first.run_dir / "states").glob("*.json"))
    assert len(snapshots) == 1
    assert content_hash(snapshots[0].read_bytes()) == first.state_hash
    assert content_hash(trace) == first.trace_hash
    events = [Event.model_validate_json(row) for row in trace.splitlines()]
    assert [event.event_type for event in events] == [
        "snapshot_loaded",
        "tool_result",
        "recovery_proposal",
        "graded",
    ]
    previous_hash = None
    for expected_sequence, event in enumerate(events):
        assert event.sequence == event.logical_time == expected_sequence
        assert event.state_before_hash == event.state_after_hash == first.state_hash
        assert event.previous_event_hash == previous_hash
        payload = event.model_dump(mode="json", exclude={"integrity_hash"})
        assert event.integrity_hash == content_hash(canonical_json(payload))
        previous_hash = event.integrity_hash


def test_tampered_artifact_is_rejected_and_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert _run().exit_code == 0
    trace = next(Path("artifacts").rglob("trace.jsonl"))
    trace.write_bytes(b"tampered\n")
    result = _run()
    assert result.exit_code == 3
    assert "stored artifact differs from replay: trace.jsonl" in result.stderr
    assert trace.read_bytes() == b"tampered\n"


def test_manifest_with_unknown_version_does_not_write_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    manifest = tmp_path / "unknown.yaml"
    manifest.write_text(MANIFEST.read_text().replace("  version: 1.0.0", "  version: 9.0.0"))
    assert _run(manifest).exit_code == 2
    assert not Path("artifacts").exists()


@pytest.fixture
def fixture_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    copied = shutil.copytree(scenario.FIXTURES, tmp_path / "fixtures")
    monkeypatch.setattr(scenario, "FIXTURES", copied)
    return copied


def test_unknown_tool_version_does_not_write_artifacts(fixture_copy: Path) -> None:
    fixture = fixture_copy / "case_read.yaml"
    fixture.write_text(fixture.read_text().replace("version: 1.0.0}", "version: 2.0.0}"))
    assert _run().exit_code == 2
    assert not Path("artifacts").exists()


def test_accept_cannot_override_failed_assertion(fixture_copy: Path) -> None:
    fixture = fixture_copy / "case_read.yaml"
    fixture.write_text(fixture.read_text().replace("equals: open", "equals: closed"))
    assert _run().exit_code == 1
    trace = next(Path("artifacts").rglob("trace.jsonl"))
    assert json.loads(trace.read_bytes().splitlines()[2])["payload"]["action"] == "ACCEPT"
    metrics = json.loads((trace.parent / "metrics.json").read_bytes())
    assert metrics["task_success"] is False
    assert metrics["assertions"][0]["evidence"] == {
        "actual": "open",
        "expected": "closed",
        "found": True,
    }


def test_bundled_tool_contract_matches_foundation_example() -> None:
    example = parse_yaml_record(Path("examples/foundation/tool.yaml"), ToolContract)
    bundled = parse_yaml_record(scenario.FIXTURES / "cases_get.yaml", ToolContract)
    assert example == bundled
