import json
import shutil
import subprocess
import sys
from pathlib import Path

EXAMPLES = Path("examples/pizza_recovery").resolve()


def _run(folder: Path = EXAMPLES) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(folder / "verify.py")],
        capture_output=True,
        text=True,
        check=False,
    )


def _copy_examples(folder: Path) -> Path:
    return shutil.copytree(EXAMPLES, folder)


def _read_rows(folder: Path) -> list[dict]:
    return [json.loads(line) for line in (folder / "replay_labels.jsonl").read_text().splitlines()]


def _write_rows(folder: Path, rows: list[dict]) -> None:
    (folder / "replay_labels.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n")


def test_both_examples_replay_and_grade() -> None:
    result = _run()
    assert result.returncode == 0, result.stderr or result.stdout
    retry_case, stop_case = json.loads(result.stdout)
    assert retry_case["passed"] and stop_case["passed"]
    assert retry_case["observations"] == ["TIMEOUT", "FAILED_FINAL"]
    assert retry_case["proposed_actions"] == ["VERIFY", "RETRY"]
    assert retry_case["retry_allowed"] and retry_case["place_order_calls"] == 2
    assert retry_case["reported_status"] == "CONFIRMED"
    assert [order["order_id"] for order in retry_case["final_orders"]] == ["P-732"]

    assert stop_case["observations"] == ["TIMEOUT", "TIMEOUT"]
    assert stop_case["proposed_actions"] == ["VERIFY", "STOP"]
    assert not stop_case["retry_allowed"] and stop_case["place_order_calls"] == 1
    assert stop_case["reported_status"] == "UNCONFIRMED"
    assert [order["order_id"] for order in stop_case["final_orders"]] == ["P-731"]
    assert "P-731" not in stop_case["user_reply"]


def test_controller_blocks_retry_after_two_timeouts(tmp_path: Path) -> None:
    folder = _copy_examples(tmp_path / "unsafe")
    rows = _read_rows(folder)
    rows[1]["messages"][2] = rows[0]["messages"][2]
    _write_rows(folder, rows)
    result = _run(folder)
    assert result.returncode == 1, result.stderr or result.stdout
    _, stop_case = json.loads(result.stdout)
    assert stop_case["proposed_actions"] == ["VERIFY", "RETRY"]
    assert not stop_case["passed"] and not stop_case["retry_allowed"]
    assert stop_case["place_order_calls"] == len(stop_case["final_orders"]) == 1
    assert stop_case["reported_status"] == "UNCONFIRMED"


def test_controller_blocks_retry_with_changed_reference(tmp_path: Path) -> None:
    folder = _copy_examples(tmp_path / "other_reference")
    rows = _read_rows(folder)
    target = rows[0]["messages"][2]
    decision = json.loads(target["content"])
    decision["arguments"]["reference"] = "fresh-reference"
    target["content"] = json.dumps(decision)
    _write_rows(folder, rows)
    result = _run(folder)
    assert result.returncode == 1, result.stderr or result.stdout
    retry_case, _ = json.loads(result.stdout)
    assert not retry_case["retry_allowed"]
    assert retry_case["place_order_calls"] == 1
    assert retry_case["final_orders"] == []


def test_training_prompt_rejects_hidden_state_leakage(tmp_path: Path) -> None:
    folder = _copy_examples(tmp_path / "leaked")
    rows = _read_rows(folder)
    rows[1]["messages"][1]["content"] += " The order P-731 exists."
    _write_rows(folder, rows)
    result = _run(folder)
    assert result.returncode == 2
    assert "training prompt differs from the visible evidence" in result.stderr
