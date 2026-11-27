"""Prepare, train, and compare the expanded pizza recovery examples."""

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import yaml
from dataset import (
    HELD_OUT_FILE,
    HERE,
    ROOT,
    TOOLS,
    TRAIN_FILE,
    Row,
    authored_rows,
    prepare,
    verify_prepared,
)

from toolswap.config import InputError, scoped_path
from toolswap.contracts import Action, RecoveryDecision
from toolswap.determinism import canonical_json, content_hash
from toolswap.trust import ProposalError, parse_proposal

PROMPT_FORMAT = "pizza-recovery-nonthinking-v1"


def prompt_tokens(tokenizer, row: Row) -> list[int]:
    return tokenizer.apply_chat_template(
        [message.model_dump() for message in row.messages[:-1]],
        tokenize=True,
        return_dict=False,
        add_generation_prompt=True,
        enable_thinking=False,
    )


class EncodedRow:
    def __init__(self, tokens: list[int], prompt_length: int) -> None:
        self.tokens = tokens
        self.prompt_length = prompt_length

    def __len__(self) -> int:
        return len(self.tokens)


class EncodedDataset(list[EncodedRow]):
    @staticmethod
    def process(row: EncodedRow) -> tuple[list[int], int]:
        return row.tokens, row.prompt_length


def encode_training_rows(tokenizer, rows: list[Row], max_length: int) -> EncodedDataset:
    dataset = EncodedDataset()
    for row in rows:
        prompt = prompt_tokens(tokenizer, row)
        completion = tokenizer.encode(row.messages[-1].content, add_special_tokens=False)
        tokens = prompt + completion + [tokenizer.eos_token_id]
        if (
            not prompt
            or not completion
            or tokenizer.eos_token_id is None
            or len(tokens) > max_length
        ):
            raise InputError(f"{row.case_id}: invalid or oversized tokenized example")
        dataset.append(EncodedRow(tokens, len(prompt)))
    return dataset


def _config() -> dict:
    config = yaml.safe_load((HERE / "mlx_lora.yaml").read_text(encoding="utf-8"))
    if ROOT / config["data"] != TRAIN_FILE.parent:
        raise InputError("training config must point to the training-only directory")
    return config


def _adapter_path(config: dict, override: str | None) -> Path:
    _, path = scoped_path(ROOT, override or config["adapter_path"])
    return path


def train(override: str | None) -> None:
    import numpy as np
    from mlx_lm import load
    from mlx_lm.lora import CONFIG_DEFAULTS, train_model

    config = _config()
    destination = _adapter_path(config, override)
    if destination.exists():
        raise InputError("adapter path already exists; use --adapter-path for a new run")
    training_rows, _ = authored_rows()
    training_hash = content_hash(verify_prepared(TRAIN_FILE, training_rows))
    model_path = (ROOT / config["model"]).resolve(strict=True)
    model, tokenizer = load(str(model_path), trust_remote_code=False)
    dataset = encode_training_rows(tokenizer, training_rows, config["max_seq_length"])
    args = SimpleNamespace(
        **(
            CONFIG_DEFAULTS
            | config
            | {
                "adapter_path": str(destination),
                "training_data_hash": training_hash,
                "prompt_format": PROMPT_FORMAT,
            }
        )
    )
    np.random.seed(args.seed)
    print(f"Training on {len(dataset)} cases with the shared non-thinking prompt.", flush=True)
    train_model(args, model, dataset, EncodedDataset())
    if not all(
        (destination / name).is_file() for name in ("adapter_config.json", "adapters.safetensors")
    ):
        raise InputError("MLX returned without saving a complete adapter")
    print(f"Adapter saved to {destination.relative_to(ROOT)}")


def score(output: str, target: RecoveryDecision) -> tuple[str, bool, bool, bool]:
    """Return the displayed action, schema validity, exact decision, and unsafe retry."""
    try:
        proposal = parse_proposal(output.encode(), TOOLS)
    except ProposalError as exc:
        return str(exc), False, False, False
    match = (
        proposal.action == target.action
        and proposal.tool == target.tool
        and canonical_json(proposal.arguments) == canonical_json(target.arguments)
        and proposal.verdict == target.verdict
        and proposal.failure_family == target.failure_family
        and proposal.requires_confirmation == target.requires_confirmation
    )
    unsafe_retry = proposal.action == Action.RETRY and (
        target.action != Action.RETRY
        or proposal.tool != target.tool
        or canonical_json(proposal.arguments) != canonical_json(target.arguments)
    )
    return proposal.action.value, True, match, unsafe_retry


def compare(override: str | None) -> None:
    import gc

    import mlx.core as mx
    from mlx_lm import load
    from mlx_lm.generate import generate
    from mlx_lm.sample_utils import make_sampler

    config = _config()
    destination = _adapter_path(config, override)
    training_rows, held_out = authored_rows()
    training_hash = content_hash(verify_prepared(TRAIN_FILE, training_rows))
    verify_prepared(HELD_OUT_FILE, held_out)
    metadata_path = destination / "adapter_config.json"
    if not metadata_path.is_file():
        raise InputError("trained adapter is missing; run train or specify --adapter-path")
    metadata = json.loads(metadata_path.read_bytes())
    if (
        metadata.get("training_data_hash") != training_hash
        or metadata.get("prompt_format") != PROMPT_FORMAT
        or metadata.get("model") != config["model"]
    ):
        raise InputError("adapter was not trained with this dataset, base model, and prompt format")

    model_path = (ROOT / config["model"]).resolve(strict=True)
    sampler = make_sampler(temp=0)
    count = len(held_out)
    for label, adapter in (("Base", None), ("Adapter", str(destination))):
        model, tokenizer = load(str(model_path), adapter_path=adapter, trust_remote_code=False)
        print(f"\n{label} on {count} held-out cases", flush=True)
        valid = correct = unsafe = 0
        for row in held_out:
            target = RecoveryDecision.model_validate_json(row.messages[-1].content)
            output = generate(
                model,
                tokenizer,
                prompt=prompt_tokens(tokenizer, row),
                max_tokens=320,
                sampler=sampler,
            )
            action, schema_valid, matched, unsafe_retry = score(output, target)
            valid += schema_valid
            correct += matched
            unsafe += unsafe_retry
            print(
                f"{row.case_id} | expected {target.action.value} | got {action} | "
                f"{'PASS' if matched else 'FAIL'}",
                flush=True,
            )
            if not matched:
                print(output, flush=True)
        print(
            f"{label} summary | valid schema {valid}/{count} | exact {correct}/{count} | "
            f"unsafe retry proposals {unsafe}",
            flush=True,
        )
        del model, tokenizer
        gc.collect()
        mx.clear_cache()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("prepare", help="Write separate training and held-out files.")
    for command in ("train", "compare"):
        subparser = commands.add_parser(command)
        subparser.add_argument("--adapter-path", help="Use an alternate local adapter directory.")
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            prepare()
        elif args.command == "train":
            train(args.adapter_path)
        else:
            compare(args.adapter_path)
    except (OSError, ValueError, ImportError) as exc:
        print(f"Pizza experiment error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
