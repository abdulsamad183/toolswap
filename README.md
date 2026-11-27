# ToolSwap

ToolSwap is a local reliability benchmark for recovery from tool failures in stateful service operations. This foundation provides a CLI for validating tool contracts and resolving run configuration.

## Run the foundation checks

With Python 3.12 and uv available on macOS or Linux, run these commands from the repository root:

```sh
uv sync --locked
uv run --locked toolswap --help
uv run --locked toolswap config resolve examples/foundation/run.yaml
uv run --locked toolswap contract validate examples/foundation/tool.yaml
bash scripts/check.sh
```

`config resolve` prints canonical JSON to stdout. `contract validate` prints the validated tool identifier and version. Both return `0` on success, `2` for invalid input, and `3` for an I/O failure. Exit `1` is reserved for evaluated failures and failed gates.

See [foundation decisions](docs/foundation.md) for scope, metrics, compatibility, and trust boundaries, and [delivery evidence](docs/foundation-evidence.md) for tracker references and checks.
