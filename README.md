# ToolSwap

ToolSwap is a local reliability benchmark for recovery from tool failures in stateful service operations. The first runnable scenario reads a mock support case and records evidence that can be replayed.

## Run the foundation checks

With Python 3.12 and uv available on macOS or Linux, run these commands from the repository root:

```sh
uv sync --locked
uv run --locked toolswap --help
uv run --locked toolswap config resolve examples/foundation/run.yaml
uv run --locked toolswap contract validate examples/foundation/tool.yaml
uv run --locked toolswap scenario run --manifest examples/feasibility/run.yaml
bash scripts/check.sh
```

`config resolve` prints canonical JSON to stdout. `contract validate` prints the validated tool identifier and version. Both return `0` on success, `2` for invalid input, and `3` for an I/O failure. Exit `1` is reserved for evaluated failures and failed gates.

Run the scenario command twice. Both runs should print `Outcome: PASS` and the same state and trace hashes. The printed artifact directory contains the tool result and stub proposal in `trace.jsonl` and the evaluator's assertion in `metrics.json`.

See [feasibility checks](docs/feasibility.md) for the Apple Silicon model commands, [foundation decisions](docs/foundation.md) for scope and compatibility, and [foundation evidence](docs/foundation-evidence.md) for the previous batch.

See the [pizza timeout examples](examples/pizza_recovery/README.md) for two mock recovery replays and a small local training exercise.

See the [SQLite state safety checks](docs/state-safety.md) for the mock state store, effect evidence, and manual verification.
