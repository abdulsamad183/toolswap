import importlib
import json
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from toolswap.contracts import RecoveryDecision

EXAMPLES = Path("examples/pizza_recovery").resolve()


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_prepare_is_deterministic_and_splits_are_isolated(tmp_path: Path) -> None:
    folder = tmp_path / "examples" / "pizza_recovery"
    shutil.copytree(EXAMPLES, folder)
    command = [sys.executable, str(folder / "experiment.py"), "prepare"]
    first_run = subprocess.run(command, capture_output=True, text=True, check=False)
    assert first_run.returncode == 0, first_run.stderr
    root = tmp_path / "artifacts" / "pizza-recovery" / "dataset"
    train_path = root / "train" / "train.jsonl"
    held_out_path = root / "held_out" / "cases.jsonl"
    assert list(train_path.parent.iterdir()) == [train_path]
    train, held_out = _rows(train_path), _rows(held_out_path)
    assert len(train) == 24 and len(held_out) == 8
    assert {row["case_id"] for row in train}.isdisjoint(row["case_id"] for row in held_out)
    assert len({row["messages"][0]["content"] for row in train + held_out}) == 1
    for rows, count in ((train, 12), (held_out, 4)):
        targets = [
            RecoveryDecision.model_validate_json(row["messages"][2]["content"]) for row in rows
        ]
        assert Counter(target.action.value for target in targets) == {"RETRY": count, "STOP": count}
        for row, target in zip(rows, targets, strict=True):
            observed = json.loads(row["messages"][1]["content"])
            assert set(observed) == {
                "user_task",
                "original_order",
                "place_order_result",
                "status_lookup",
                "status_checks_remaining",
                "retries_remaining",
                "contract_allows_retry",
            }
            if target.action == "RETRY":
                assert target.arguments == observed["original_order"]
                assert observed["status_lookup"]["order_created"] is False
                assert observed["status_lookup"]["attempt_can_still_complete"] is False
                assert observed["retries_remaining"] > 0 and observed["contract_allows_retry"]
            else:
                assert target.tool is None and target.arguments == {}
    outputs = (train_path.read_bytes(), held_out_path.read_bytes())
    second_run = subprocess.run(command, capture_output=True, text=True, check=False)
    assert second_run.returncode == 0, second_run.stderr
    assert outputs == (train_path.read_bytes(), held_out_path.read_bytes())


def test_training_packs_same_prompt_as_comparison(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.syspath_prepend(str(EXAMPLES))
    experiment = importlib.import_module("experiment")
    train, _ = experiment.authored_rows()

    class Tokenizer:
        eos_token_id = 99

        def apply_chat_template(self, messages, **options):
            assert [message["role"] for message in messages] == ["system", "user"]
            assert options["enable_thinking"] is False and options["add_generation_prompt"]
            return [1, 2, 3]

        def encode(self, text, *, add_special_tokens):
            assert not add_special_tokens
            assert RecoveryDecision.model_validate_json(text).action == "RETRY"
            return [4, 5]

    tokenizer = Tokenizer()
    dataset = experiment.encode_training_rows(tokenizer, train[:1], max_length=6)
    tokens, offset = dataset.process(dataset[0])
    assert tokens[:offset] == experiment.prompt_tokens(tokenizer, train[0])
    assert tokens[offset:] == [4, 5, tokenizer.eos_token_id]


def test_comparison_scores_bad_schema_and_unsafe_retry_separately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(EXAMPLES))
    experiment = importlib.import_module("experiment")
    _, held_out = experiment.authored_rows()
    retry = held_out[0].messages[-1].content
    stop = held_out[-1].messages[-1].content
    target = RecoveryDecision.model_validate_json(stop)
    assert experiment.score(stop, target) == ("STOP", True, True, False)
    assert experiment.score(retry, target) == ("RETRY", True, False, True)
    assert experiment.score('{"decision": {"action": "retry_order"}}', target) == (
        "invalid_decision",
        False,
        False,
        False,
    )
