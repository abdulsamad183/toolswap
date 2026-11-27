# Pizza timeout examples

The two teaching replays use simulated order state and scripted proposals from
`replay_labels.jsonl`. The expanded training experiment uses a separate set of
authored cases in `decision_cases.yaml`.

## Replay and inspect

Run from the repository root with Python 3.12 and uv:

```sh
uv run --locked python examples/pizza_recovery/verify.py
```

The output includes the observed results, proposed actions, whether the retry was
allowed, the final fake orders, and the proposed user reply. Both checks should
show `"passed": true`.

| Scenario | Status lookup | Actions | Create calls | Final order count | Reported status |
| --- | --- | --- | ---: | ---: | --- |
| `pizza_002` | `FAILED_FINAL` | `VERIFY`, `RETRY` | 2 | 1 | `CONFIRMED` |
| `pizza_003` | `TIMEOUT` | `VERIFY`, `STOP` | 1 | 1 | `UNCONFIRMED` |

For regression checks, including attempts to retry with two timeouts or a changed
reference, run `uv run --locked pytest -q tests/test_pizza_examples.py`.

## Prepare the expanded dataset

```sh
uv run --locked python examples/pizza_recovery/experiment.py prepare
```

This generates 24 training rows and eight held-out rows, with equal numbers of
`RETRY` and `STOP` in each split. Cases vary the order details and the evidence
from the status lookup. `STOP` includes timeouts, pending or incomplete results,
and definitive failures where the retry budget or tool contract blocks another
attempt. The training and held-out files are in separate directories under
`artifacts/pizza-recovery/dataset/`.

## Train locally on Apple Silicon

Use the prepared Qwen3-4B model from the
[feasibility guide](../../docs/feasibility.md). Run these commands from the repository root:

```sh
uv sync --locked --extra mlx
HF_HUB_OFFLINE=1 uv run --locked --extra mlx python examples/pizza_recovery/experiment.py train
```

The runner uses `mlx_lora.yaml`, masks the prompt in the loss, and runs 72
optimizer steps at batch size one, or three passes through the 24 examples. It
saves the adapter in `artifacts/pizza-recovery/adapter-expanded/`. For a separate
run, pass `--adapter-path artifacts/pizza-recovery/adapter-expanded-2` to both
`train` and `compare`. The held-out rows are reserved for comparison after training.

## Compare with the base model

```sh
HF_HUB_OFFLINE=1 uv run --locked --extra mlx python examples/pizza_recovery/experiment.py compare
```

This runs the base model and the newly trained adapter on the same eight held-out
inputs with greedy decoding. Both use the same system instructions, structured
evidence, and non-thinking Qwen chat template as training. The expected assistant
answer stays out of the prompt. The output shows each expected and actual action,
schema validity, exact decisions, and unsafe retry proposals. Exact decisions
include the action, tool, original arguments, verdict, failure family, and
confirmation flag. The harness scores proposals from simulated cases.

Use the base result as the reference. An adapter improvement means more exact
decisions without introducing unsafe retries. Eight cases give a small, local
comparison of these pizza recovery rules.
