import json
from pathlib import Path
from tempfile import TemporaryDirectory

from hypothesis import example, given, settings
from hypothesis import strategies as st

from toolswap.config import parse_yaml_record, resolve_config
from toolswap.contracts import Scenario
from toolswap.determinism import canonical_json, content_hash
from toolswap.m1 import run_clean_scenario
from toolswap.scenario import FIXTURES
from toolswap.service_contracts import tool_reference
from toolswap.service_operations import build_service_registry
from toolswap.state import StateStore

MAX_SEED = 2**64 - 1
SEEDS = st.integers(min_value=0, max_value=MAX_SEED)
STOCK = {"sku": "mock-part-1", "location": "north"}
SNAPSHOT = FIXTURES / "state_safety_snapshot.json"


def _reset(folder: Path, seed: int, *, available: int = 2, reserved: int = 0) -> StateStore:
    scenario = parse_yaml_record(FIXTURES / "state_safety.yaml", Scenario)
    data = json.loads(SNAPSHOT.read_bytes())
    data["tables"]["inventory"][0].update(available_count=available, reserved_count=reserved)
    payload = canonical_json(data)
    scenario = scenario.model_copy(update={"initial_snapshot": content_hash(payload)})
    return StateStore.reset(folder / "state.sqlite3", scenario, payload, seed=seed)


@settings(max_examples=18, deadline=None)
@given(seed=SEEDS)
@example(seed=0)
@example(seed=MAX_SEED)
def test_clean_replay_is_deterministic_for_a_manifest_seed(seed: int) -> None:
    with TemporaryDirectory() as scratch:
        root = Path(scratch)
        manifest = resolve_config(Path("examples/m1/replacement.yaml"), root).model_copy(
            update={"seed": seed}
        )
        one = run_clean_scenario(manifest, root / "one", FIXTURES)
        two = run_clean_scenario(manifest, root / "two", FIXTURES)
        assert one.passed and two.passed
        assert (one.run_dir.name, one.state_hash, one.trace_hash) == (
            two.run_dir.name,
            two.state_hash,
            two.trace_hash,
        )
        assert (one.run_dir / "metrics.json").read_bytes() == (
            two.run_dir / "metrics.json"
        ).read_bytes()
        stored = json.loads((one.run_dir / "resolved_config.json").read_bytes())
        assert stored["seed"] == seed


@settings(max_examples=28, deadline=None)
@given(
    seed=SEEDS,
    available=st.integers(0, 20),
    reserved=st.integers(0, 20),
    quantity=st.integers(1, 21),
    reserve=st.booleans(),
)
@example(seed=0, available=0, reserved=0, quantity=1, reserve=True)
@example(seed=MAX_SEED, available=0, reserved=20, quantity=20, reserve=False)
def test_generated_inventory_transfers_preserve_bounds_and_are_idempotent(
    seed: int,
    available: int,
    reserved: int,
    quantity: int,
    reserve: bool,
) -> None:
    with TemporaryDirectory() as scratch:
        store = _reset(Path(scratch), seed, available=available, reserved=reserved)
        registry = build_service_registry()
        tool = tool_reference("inventory.reserve" if reserve else "inventory.release")
        args = {**STOCK, "quantity": quantity}
        before = store.current_state()
        result = registry.invoke(store, tool, args, request_key="transfer")
        if quantity > (available if reserve else reserved):
            assert result.error is not None and result.error["code"] == "precondition_failed"
            assert store.current_state() == before and not store.effects()
            return

        expected_available = available - quantity if reserve else available + quantity
        expected_reserved = reserved + quantity if reserve else reserved - quantity
        assert result.observation == {
            **STOCK,
            "available_count": expected_available,
            "reserved_count": expected_reserved,
        }
        assert expected_available >= 0 and expected_reserved >= 0
        assert expected_available + expected_reserved == available + reserved
        committed = store.current_state()
        duplicate = registry.invoke(store, tool, args, request_key="transfer")
        assert duplicate.observation == result.observation
        assert [effect.status for effect in store.effects()] == ["complete", "duplicate"]
        assert store.current_state() == committed
        changed = registry.invoke(
            store, tool, {**args, "quantity": quantity + 1}, request_key="transfer"
        )
        assert changed.error is not None and changed.error["code"] == "idempotency_conflict"
        assert store.current_state() == committed and len(store.effects()) == 2


@settings(max_examples=15, deadline=None)
@given(seed=SEEDS, suffix=st.integers(min_value=0, max_value=10_000))
def test_generated_keys_reapply_each_case_order_and_delivery_effect_once(
    seed: int, suffix: int
) -> None:
    with TemporaryDirectory() as scratch:
        store = _reset(Path(scratch), seed)
        registry = build_service_registry()
        case = {"case_id": "mock-case-1"}
        for name, args in (
            (
                "cases.update",
                {**case, "expected_notes": "Replacement approved", "notes": f"Queued {suffix}"},
            ),
            ("work_orders.create", {**case, "action": "replace"}),
            ("notifications.send", {**case, "channel": "email"}),
        ):
            tool = tool_reference(name)
            key = f"{name}-{suffix}"
            applied = registry.invoke(store, tool, args, request_key=key)
            committed = store.current_state()
            repeated = registry.invoke(store, tool, args, request_key=key)
            assert applied.effect is not None and applied.effect.status == "complete"
            assert repeated.effect is not None and repeated.effect.status == "duplicate"
            assert applied.observation == repeated.observation
            assert store.current_state() == committed
        tables = json.loads(store.resolve_snapshot(store.current_state().state_hash))["tables"]
        assert len(tables["work_orders"]) == len(tables["notifications"]) == 1
        assert [row.status for row in store.effects()] == ["complete", "duplicate"] * 3
