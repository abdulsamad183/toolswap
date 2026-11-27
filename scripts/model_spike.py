"""Prepare pinned Qwen weights and run the Apple Silicon feasibility check."""

import argparse
import json
import math
import os
import platform
import subprocess
import sys
from errno import EEXIST, ENOTEMPTY
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory, mkdtemp
from time import perf_counter

from toolswap.contracts import Action
from toolswap.determinism import canonical_json, content_hash
from toolswap.trust import ProposalError, parse_proposal

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "models" / "feasibility"
ARTIFACTS = ROOT / "artifacts" / "model-feasibility"
RELEASE = "0.32.2"
CANDIDATES = {
    "4b": ("Qwen/Qwen3-4B", "1cfa9a7208912126459214e8b04321603b3df60c"),
    "1.7b": ("Qwen/Qwen3-1.7B", "70d244cc86ccca08cf5af4e1e306ecf908b1ad5e"),
}
QUANTIZATION = {"bits": 4, "group_size": 64, "mode": "affine"}
MEMORY_LIMIT = 28_000_000_000
SEED = 0
MAX_SEQUENCE = 2048
MAX_OUTPUT = 256
LEARNING_RATE = 1e-4
ADAPTER_CONFIG = {
    "fine_tune_type": "lora",
    "num_layers": 4,
    "lora_parameters": {
        "rank": 8,
        "scale": 20.0,
        "dropout": 0.0,
        "keys": ["self_attn.q_proj", "self_attn.v_proj"],
    },
}
DECISION = canonical_json(
    {
        "schema_version": "1.0.0",
        "verdict": "RESULT_ACCEPTABLE",
        "failure_family": None,
        "confidence": 1.0,
        "evidence": ["The case is open."],
        "action": "ACCEPT",
        "tool": None,
        "arguments": {},
        "expected_postcondition": None,
        "requires_confirmation": False,
    }
).decode()
MESSAGES = [
    {"role": "system", "content": "Output only the requested JSON object, without markdown."},
    {
        "role": "user",
        "content": "The mock case read returned status open. Return this exact recovery decision:\n"
        + DECISION,
    },
]


def _require_mlx() -> None:
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise RuntimeError("The model feasibility check requires native Apple Silicon Python.")
    if version("mlx-lm") != RELEASE:
        raise RuntimeError(f"Install the locked mlx extra for MLX LM {RELEASE}.")


def _model_dir(candidate: str) -> Path:
    return MODELS / candidate / CANDIDATES[candidate][1]


def _identity(candidate: str) -> dict:
    repo, revision = CANDIDATES[candidate]
    return {
        "schema_version": "1.0.0",
        "repository": repo,
        "model_revision": revision,
        "tokenizer_revision": revision,
        "license": "Apache-2.0",
        "mlx_lm_version": RELEASE,
        "quantization": QUANTIZATION,
    }


def _json(path: Path, payload: dict) -> None:
    path.write_bytes(canonical_json(payload) + b"\n")


def _prepared(candidate: str) -> tuple[Path, dict]:
    folder = _model_dir(candidate)
    if not (folder / "preparation.json").is_file():
        raise RuntimeError(f"Prepare the pinned {candidate} checkpoint first.")
    manifest = json.loads((folder / "preparation.json").read_bytes())
    if any(manifest.get(key) != value for key, value in _identity(candidate).items()):
        raise RuntimeError(f"Prepared {candidate} checkpoint does not match its pinned identity.")
    if not (folder / "config.json").is_file() or not list(folder.glob("*.safetensors")):
        raise RuntimeError(f"Prepared {candidate} checkpoint is incomplete.")
    tokenizer_hash = content_hash((folder / "tokenizer_config.json").read_bytes())
    if tokenizer_hash != manifest.get("tokenizer_hash"):
        raise RuntimeError(f"Prepared {candidate} tokenizer checksum mismatch.")
    return folder, manifest


def prepare(candidate: str) -> None:
    import mlx.core as mx
    from huggingface_hub import snapshot_download
    from mlx_lm.convert import convert

    target = _model_dir(candidate)
    if target.exists():
        _prepared(candidate)
        print(f"Reusing the prepared {candidate} checkpoint.", flush=True)
        return

    repo, revision = CANDIDATES[candidate]
    print(f"Downloading {repo}@{revision}", flush=True)
    snapshot = Path(
        snapshot_download(
            repo_id=repo,
            revision=revision,
            cache_dir=MODELS / "hub",
            token=False,
            allow_patterns=["*.json", "*.safetensors", "merges.txt", "vocab.json", "LICENSE"],
            max_workers=4,
        )
    )
    if snapshot.name != revision or not (snapshot / "LICENSE").is_file():
        raise RuntimeError("The downloaded snapshot lacks the pinned revision or license.")
    target.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=target.parent) as staging:
        converted = Path(staging) / "checkpoint"
        mx.reset_peak_memory()
        start = perf_counter()
        convert(
            str(snapshot),
            str(converted),
            quantize=True,
            q_bits=QUANTIZATION["bits"],
            q_group_size=QUANTIZATION["group_size"],
            q_mode=QUANTIZATION["mode"],
            trust_remote_code=False,
        )
        source_tokenizer_hash = content_hash((snapshot / "tokenizer_config.json").read_bytes())
        _json(
            converted / "preparation.json",
            _identity(candidate)
            | {
                "source_tokenizer_hash": source_tokenizer_hash,
                "tokenizer_hash": content_hash((converted / "tokenizer_config.json").read_bytes()),
                "conversion_seconds": perf_counter() - start,
                "conversion_peak_memory_bytes": mx.get_peak_memory(),
            },
        )
        try:
            converted.rename(target)
        except OSError as exc:
            if exc.errno not in {EEXIST, ENOTEMPTY}:
                raise
            _prepared(candidate)


def _train(model, tokenizer, prompt: list[int], directory: Path) -> dict:
    import mlx.core as mx
    import mlx.nn as nn
    import mlx.optimizers as optim
    from mlx.utils import tree_flatten
    from mlx_lm.tuner.utils import linear_to_lora_layers

    response = tokenizer.encode(DECISION, add_special_tokens=False) + [tokenizer.eos_token_id]
    if len(prompt) + len(response) > MAX_SEQUENCE:
        raise RuntimeError("The untruncated training example exceeds the configured context.")
    tokens = mx.array([prompt + response])
    model.freeze()
    linear_to_lora_layers(model, ADAPTER_CONFIG["num_layers"], ADAPTER_CONFIG["lora_parameters"])
    model.train()
    parameters = dict(tree_flatten(model.trainable_parameters()))
    if not parameters or any(not name.endswith((".lora_a", ".lora_b")) for name in parameters):
        raise RuntimeError("The model did not expose only the expected LoRA parameters.")
    optimizer = optim.Adam(learning_rate=LEARNING_RATE)

    def completion_loss(model, tokens):
        # The last prompt token predicts the first response token.
        logits = model(tokens[:, :-1])[:, len(prompt) - 1 :, :].astype(mx.float32)
        return nn.losses.cross_entropy(logits, tokens[:, len(prompt) :]).mean()

    loss, gradients = nn.value_and_grad(model, completion_loss)(model, tokens)
    mx.eval(loss)
    if not math.isfinite(loss.item()):
        raise RuntimeError("The one-step training loss is nonfinite.")
    optimizer.update(model, gradients)
    weights = dict(tree_flatten(model.trainable_parameters()))
    mx.eval(weights, optimizer.state)
    if optimizer.state["step"].item() != 1 or not any(
        (weights[name] != value).any().item() for name, value in parameters.items()
    ):
        raise RuntimeError("The optimizer did not change the adapter in exactly one step.")
    if not all(mx.isfinite(value).all().item() for value in weights.values()):
        raise RuntimeError("The updated adapter contains nonfinite values.")

    directory.mkdir()
    _json(directory / "adapter_config.json", ADAPTER_CONFIG)
    weights_path = directory / "adapters.safetensors"
    mx.save_safetensors(str(weights_path), weights)
    return {
        "optimizer_updates": 1,
        "completion_tokens": len(response),
        "loss": loss.item(),
        "adapter_hash": content_hash(weights_path.read_bytes()),
        "adapter_config_hash": content_hash((directory / "adapter_config.json").read_bytes()),
    }


def _infer(model, tokenizer, prompt: list[int], repeats: int) -> dict:
    from mlx_lm.generate import generate
    from mlx_lm.sample_utils import make_sampler

    sampler = make_sampler(temp=0)
    outputs = [
        generate(model, tokenizer, prompt=prompt, max_tokens=MAX_OUTPUT, sampler=sampler)
        for _ in range(repeats)
    ]
    validated = []
    for output in outputs:
        try:
            proposal = parse_proposal(output.encode(), {"cases.get"})
            validated.append(proposal.action == Action.ACCEPT)
        except ProposalError:
            validated.append(False)
    return {"outputs": outputs, "identical": len(set(outputs)) == 1, "valid": all(validated)}


def _worker(candidate: str, stage: str, destination: Path) -> None:
    import mlx.core as mx
    from mlx_lm import load

    folder, identity = _prepared(candidate)
    adapter = destination / "adapter"
    mx.random.seed(SEED)
    mx.reset_peak_memory()
    start = perf_counter()
    model, tokenizer = load(
        str(folder),
        adapter_path=str(adapter) if stage == "reload" else None,
        trust_remote_code=False,
        tokenizer_config={"trust_remote_code": False, "local_files_only": True},
    )
    loaded = perf_counter()
    prompt = tokenizer.apply_chat_template(
        MESSAGES,
        tokenize=True,
        return_dict=False,
        add_generation_prompt=True,
        enable_thinking=False,
    )
    if not prompt or len(prompt) + MAX_OUTPUT > MAX_SEQUENCE:
        raise RuntimeError("The tokenized prompt does not fit the spike context.")
    if stage == "train":
        data = _train(model, tokenizer, prompt, adapter)
    else:
        data = _infer(model, tokenizer, prompt, repeats=2 if stage == "inference" else 1)
        if stage == "reload":
            data |= {
                "adapter_hash": content_hash((adapter / "adapters.safetensors").read_bytes()),
                "adapter_config_hash": content_hash((adapter / "adapter_config.json").read_bytes()),
            }
    _json(
        destination / f"{stage}.json",
        {
            "identity": identity,
            "prompt_hash": content_hash(canonical_json(prompt)),
            "prompt_tokens": len(prompt),
            "load_seconds": loaded - start,
            "stage_seconds": perf_counter() - loaded,
            "peak_memory_bytes": mx.get_peak_memory(),
        }
        | data,
    )


def _run_worker(candidate: str, stage: str, destination: Path) -> dict:
    print(f"Checking {candidate}: {stage}", flush=True)
    result = subprocess.run(
        [sys.executable, __file__, "_worker", "--candidate", candidate, stage, str(destination)],
        env=os.environ | {"HF_HUB_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1"},
        check=False,
    )
    if result.returncode != 0:
        return {"passed": False, "worker_exit_code": result.returncode}
    data = json.loads((destination / f"{stage}.json").read_bytes())
    data["passed"] = 0 < data["peak_memory_bytes"] <= MEMORY_LIMIT and (
        stage == "train" or (data["valid"] and data["identical"])
    )
    return data


def _check_candidate(candidate: str, destination: Path, *, train: bool) -> dict:
    destination.mkdir()
    evidence = {"candidate": _identity(candidate)}
    try:
        _prepared(candidate)
    except (OSError, RuntimeError, ValueError) as exc:
        return evidence | {
            "prepared": False,
            "inference": {"passed": False, "error": str(exc)},
        }
    inference = _run_worker(candidate, "inference", destination)
    evidence |= {"prepared": True, "inference": inference}
    if train and inference["passed"]:
        training = _run_worker(candidate, "train", destination)
        evidence["train"] = training
        if training["passed"]:
            reload = _run_worker(candidate, "reload", destination)
            reload["passed"] = (
                reload["passed"]
                and inference["prompt_hash"] == training["prompt_hash"] == reload["prompt_hash"]
                and reload["adapter_hash"] == training["adapter_hash"]
                and reload["adapter_config_hash"] == training["adapter_config_hash"]
            )
            evidence["reload"] = reload
    return evidence


def check() -> int:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    directory = Path(mkdtemp(prefix="run-", dir=ARTIFACTS))
    primary = _check_candidate("4b", directory / "4b", train=True)
    primary_passed = primary.get("reload", {}).get("passed", False)
    fallback = _check_candidate(
        "1.7b", directory / "1.7b", train=primary["prepared"] and not primary_passed
    )
    selected = None
    if fallback["inference"]["passed"]:
        if primary_passed:
            selected = "4b"
        elif fallback.get("reload", {}).get("passed", False):
            selected = "1.7b"
    report = {
        "schema_version": "1.0.0",
        "selected_candidate": selected,
        "memory_limit_bytes": MEMORY_LIMIT,
        "settings": {
            "seed": SEED,
            "thinking": False,
            "temperature": 0,
            "max_sequence_length": MAX_SEQUENCE,
            "max_output_tokens": MAX_OUTPUT,
            "learning_rate": LEARNING_RATE,
            "optimizer": "Adam",
            "optimizer_updates": 1,
            "batch_size": 1,
            "gradient_accumulation_steps": 1,
            "completion_only_loss": True,
            "adapter": ADAPTER_CONFIG,
        },
        "runtime": {
            "python": platform.python_version(),
            "mlx": version("mlx"),
            "mlx_lm": version("mlx-lm"),
            "transformers": version("transformers"),
            "lock_hash": content_hash((ROOT / "uv.lock").read_bytes()),
        },
        "primary": primary,
        "fallback": fallback,
    }
    _json(directory / "report.json", report)
    print(f"Selected candidate: {selected or 'none'}")
    print(f"Report: {directory.relative_to(ROOT) / 'report.json'}")
    if not primary["prepared"] or not fallback["prepared"]:
        print("Run prepare for both checkpoints before closing the feasibility gate.")
        return 3
    return 0 if selected else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    preparation = commands.add_parser("prepare", help="Download and quantize the pinned weights.")
    preparation.add_argument("--candidate", choices=[*CANDIDATES, "all"], default="all")
    commands.add_parser("check", help="Run the offline feasibility checks.")
    worker = commands.add_parser("_worker", help="Internal worker started by check.")
    worker.add_argument("--candidate", choices=CANDIDATES, required=True)
    worker.add_argument("stage", choices=["inference", "train", "reload"])
    worker.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        _require_mlx()
        if args.operation == "_worker":
            _worker(args.candidate, args.stage, args.destination)
        elif args.operation == "check":
            raise SystemExit(check())
        else:
            candidates = CANDIDATES if args.candidate == "all" else [args.candidate]
            for candidate in candidates:
                prepare(candidate)
    except Exception as exc:
        parser.exit(3, f"{type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    main()
