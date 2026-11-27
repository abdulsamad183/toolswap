# Workflow 1 feasibility checks

## Read-only scenario

Run the scenario command in the README twice from the repository root. Compare the two printed hashes. In the reported directory, `trace.jsonl` records the snapshot, tool result, stub proposal, and evaluation in that order. `metrics.json` records the expected and actual values for the case status. The stored snapshot filename contains its SHA-256 digest. A replay reports a mismatch if the stored evidence has been edited.

## Apple Silicon model check

Run these commands from the repository root with native Python 3.12:

```sh
uv sync --locked --extra mlx
uv run --locked --extra mlx python scripts/model_spike.py prepare
uv run --locked --extra mlx python scripts/model_spike.py check
```

Preparation downloads roughly 12 GB from the two pinned public Qwen snapshots and quantizes them locally. Use `prepare --candidate 4b` or `prepare --candidate 1.7b` to prepare them individually. Prepared checkpoints are reused when their recorded identity and tokenizer metadata match. Checkpoints live under `models/feasibility/`. The check uses the prepared files in offline worker processes.

The report path printed by `check` contains the selected model, repeated inference results, per-stage MLX peak memory in bytes, and one-step training and adapter reload evidence for the selected candidate. Training uses a single purpose-built sample and measures compatibility. The report and adapter stay under the ignored `artifacts/model-feasibility/` directory. The 28 GB limit is applied to the reported MLX allocator peak of each checked stage.

## Verified result

The local Apple M3 Pro run selected Qwen3-4B. Its two base outputs matched and passed the strict decision parser. One optimizer update produced a finite training loss of `0.0500115864`, and a fresh process loaded the same adapter checksum and returned a schema-valid decision. Qwen3-1.7B also returned matching, schema-valid base outputs and remains the fallback.

| Check | Peak MLX memory |
| --- | ---: |
| 4B base inference | 2.60 GB |
| 4B one-step training | 2.70 GB |
| 4B adapter reload and inference | 2.61 GB |
| 1.7B base inference | 1.31 GB |

Two independent read-only runs passed the case-status assertion and produced matching state and trace hashes. The portable suite passed all 81 tests in a clean environment with the MLX extra absent. The installed wheel also replayed the scenario from outside the checkout.

## Tracker evidence

| Task | Acceptance evidence |
| --- | --- |
| P0-03 and P0-04 | [Merged foundation quality checks on macOS and Linux](https://github.com/abdulsamad183/toolswap/actions/runs/45971602109). |
| P0-12 | Base inference is repeatable and passes the strict recovery decision parser. |
| P0-13 | One finite, completion-only training update changes the adapter, and a fresh process reloads the recorded checksums and runs the same prompt. |
| P0-14 | The primary qualifies or the validated fallback qualifies, and the smaller checkpoint passes the base inference check. |
| P0-15 | The scenario replays with identical evidence and the model feasibility decision is recorded. |
