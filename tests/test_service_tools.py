import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pytest
from typer.testing import CliRunner

from toolswap import service_operations
from toolswap.cli import app
from toolswap.config import InputError, parse_yaml_record
from toolswap.contracts import JsonObject, Scenario
from toolswap.determinism import canonical_json, stable_id
from toolswap.service_contracts import service_contracts, tool_reference
from toolswap.service_operations import FIXTURES, build_service_registry
from toolswap.service_tools import ToolRegistry, ToolResult
from toolswap.state import EffectOutcome, StateIntegrityError, StateStore

CASE: JsonObject = {"case_id": "mock-case-1"}
CREATE: JsonObject = CASE | {"action": "replace"}
STOCK: JsonObject = {"sku": "mock-part-1", "location": "north"}
TRANSFER: JsonObject = STOCK | {"quantity": 1}
SEND: JsonObject = CASE | {"channel": "email"}


def _new_store(path: Path, seed: int = 0) -> StateStore:
    scenario = parse_yaml_record(FIXTURES / "state_safety.yaml", Scenario)
    payload = canonical_json(json.loads((FIXTURES / "state_safety_snapshot.json").read_bytes()))
    return StateStore.reset(path, scenario, payload, seed=seed)


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    return _new_store(tmp_path / "state.sqlite3")


@pytest.fixture
def registry() -> ToolRegistry:
    return build_service_registry()


def _error(result: ToolResult, code: str) -> None:
    assert result.observation is None
    assert result.error is not None
    assert set(result.error) == {"code", "message"}
    assert result.error["code"] == code
    assert result.error["message"]


def _complete(store: StateStore, result: ToolResult) -> JsonObject:
    assert result.error is None
    assert result.effect is not None and result.effect.status == "complete"
    before, after = result.effect.state_before_hash, result.effect.state_after_hash
    assert before != after == store.current_state().state_hash
    assert store.state_diff(before, after)
    assert store.effects()[-1] == result.effect
    assert result.observation is not None
    return result.observation


def test_reads_are_exact_and_do_not_write(store: StateStore, registry: ToolRegistry) -> None:
    order = _complete(
        store,
        registry.invoke(store, tool_reference("work_orders.create"), CREATE, request_key="create"),
    )
    delivery = _complete(
        store,
        registry.invoke(store, tool_reference("notifications.send"), SEND, request_key="send"),
    )
    before = store.path.read_bytes()
    state = store.current_state()
    calls = [
        (
            "accounts.get",
            "1.0.0",
            {"account_id": "mock-account-1"},
            {"account_id": "mock-account-1", "status": "active", "verified": True},
        ),
        (
            "accounts.verify",
            "1.0.0",
            {"account_id": "mock-account-2"},
            {"account_id": "mock-account-2", "verified": False},
        ),
        ("cases.get", "1.0.0", CASE, CASE | {"status": "open"}),
        (
            "cases.get",
            "2.0.0",
            CASE,
            CASE
            | {
                "status": "open",
                "account_id": "mock-account-1",
                "issue": "Replacement for a damaged part",
                "notes": "Replacement approved",
                "replacement_approved": True,
            },
        ),
        ("work_orders.get", "1.0.0", {"work_order_id": order["work_order_id"]}, order),
        ("work_orders.get_by_idempotency_key", "1.0.0", {"idempotency_key": "create"}, order),
        ("notifications.get_delivery_status", "1.0.0", {"idempotency_key": "send"}, delivery),
    ]
    for sku, location, available in (
        ("mock-part-1", "north", 2),
        ("mock-part-1", "south", 0),
        ("mock-part-2", "north", 1),
    ):
        stock = {"sku": sku, "location": location}
        calls.extend(
            [
                (
                    "inventory.get_availability",
                    "1.0.0",
                    stock,
                    stock | {"available_count": available},
                ),
                ("inventory.get_reservation_state", "1.0.0", stock, stock | {"reserved_count": 0}),
            ]
        )
    for tool, version, args, expected in calls:
        ref = tool_reference(tool, version)
        assert registry.invoke(store, ref, args) == ToolResult(observation=expected)
        missing = dict.fromkeys(args, "missing")
        _error(registry.invoke(store, ref, missing), "not_found")
    assert store.current_state() == state
    assert len(store.effects()) == 2
    assert store.path.read_bytes() == before


def test_case_writes_enforce_open_and_current_notes(
    store: StateStore, registry: ToolRegistry
) -> None:
    update = tool_reference("cases.update")
    close = tool_reference("cases.close")
    args = CASE | {"expected_notes": "Replacement approved", "notes": "Ready for dispatch"}
    updated = _complete(store, registry.invoke(store, update, args, request_key="update"))
    assert updated["notes"] == "Ready for dispatch"
    assert updated["status"] == "open"
    replay = registry.invoke(store, update, args, request_key="update")
    assert replay.observation == updated
    assert replay.effect is not None and replay.effect.status == "duplicate"
    before = store.current_state()
    _error(registry.invoke(store, update, args, request_key="stale"), "precondition_failed")
    assert store.current_state() == before
    assert store.lookup_effect("stale") is None

    closed = _complete(store, registry.invoke(store, close, CASE, request_key="close"))
    assert closed == updated | {"status": "closed"}
    _error(registry.invoke(store, close, CASE, request_key="close-again"), "precondition_failed")
    new_args = args | {"expected_notes": "Ready for dispatch", "notes": "New note"}
    _error(registry.invoke(store, update, new_args, request_key="closed"), "precondition_failed")
    assert [effect.status for effect in store.effects()] == ["complete", "duplicate", "complete"]


def test_reserve_and_release_preserve_stock(store: StateStore, registry: ToolRegistry) -> None:
    reserve = tool_reference("inventory.reserve")
    release = tool_reference("inventory.release")
    starting = store.current_state()
    _error(registry.invoke(store, release, TRANSFER, request_key="too-many"), "precondition_failed")
    _error(
        registry.invoke(store, reserve, TRANSFER | {"location": "south"}, request_key="empty"),
        "precondition_failed",
    )
    assert store.current_state() == starting and not store.effects()
    reserved = _complete(store, registry.invoke(store, reserve, TRANSFER, request_key="hold"))
    assert reserved == STOCK | {"available_count": 1, "reserved_count": 1}
    restored = _complete(store, registry.invoke(store, release, TRANSFER, request_key="free"))
    assert restored == STOCK | {"available_count": 2, "reserved_count": 0}
    assert store.current_state() == starting


@pytest.mark.parametrize("quantity", [0, -1, 1.5, True, 2**63])
def test_invalid_quantities_fail_before_dispatch(
    quantity: int | float, registry: ToolRegistry, tmp_path: Path
) -> None:
    absent = StateStore(tmp_path / "absent.sqlite3")
    _error(
        registry.invoke(
            absent,
            tool_reference("inventory.reserve"),
            TRANSFER | {"quantity": quantity},
            request_key="hold",
        ),
        "invalid_arguments",
    )
    assert not absent.path.exists()


def test_invalid_inputs_and_key_conflict_leave_state_intact(
    store: StateStore, registry: ToolRegistry
) -> None:
    create = tool_reference("work_orders.create")
    start = store.current_state()
    for key in (None, "", "   "):
        _error(registry.invoke(store, create, CREATE, request_key=key), "invalid_arguments")
    _error(
        registry.invoke(store, create, CREATE | {"extra": True}, request_key="bad"),
        "invalid_arguments",
    )
    _error(
        registry.invoke(
            store,
            tool_reference("notifications.send"),
            SEND | {"channel": "unknown"},
            request_key="channel",
        ),
        "invalid_arguments",
    )
    assert store.current_state() == start and not store.effects()
    _complete(store, registry.invoke(store, create, CREATE, request_key="existing"))
    before = store.path.read_bytes()
    _error(
        registry.invoke(store, create, CREATE | {"case_id": "mock-case-2"}, request_key="existing"),
        "idempotency_conflict",
    )
    _error(
        registry.invoke(store, tool_reference("notifications.send"), SEND, request_key="existing"),
        "idempotency_conflict",
    )
    assert store.path.read_bytes() == before


def test_create_checks_approval_and_cancel_checks_status(
    store: StateStore, registry: ToolRegistry
) -> None:
    create = tool_reference("work_orders.create")
    cancel = tool_reference("work_orders.cancel")
    _error(
        registry.invoke(
            store, create, CREATE | {"case_id": "mock-case-2"}, request_key="unapproved"
        ),
        "precondition_failed",
    )
    assert not store.effects()
    order = _complete(store, registry.invoke(store, create, CREATE, request_key="original"))
    _error(registry.invoke(store, create, CREATE, request_key="other"), "precondition_failed")
    cancel_args = {"work_order_id": order["work_order_id"]}
    cancelled = _complete(store, registry.invoke(store, cancel, cancel_args, request_key="cancel"))
    assert cancelled == order | {"status": "cancelled"}
    _error(
        registry.invoke(store, cancel, cancel_args, request_key="cancel-again"),
        "precondition_failed",
    )
    lookup = registry.invoke(
        store, tool_reference("work_orders.get_by_idempotency_key"), {"idempotency_key": "original"}
    )
    assert lookup.observation == cancelled
    new_order = _complete(store, registry.invoke(store, create, CREATE, request_key="new"))
    assert new_order["work_order_id"] != order["work_order_id"]
    _complete(
        store, registry.invoke(store, tool_reference("cases.close"), CASE, request_key="close")
    )
    _error(registry.invoke(store, create, CREATE, request_key="closed"), "precondition_failed")
    assert len(store.effects()) == 4


def test_lost_create_response_is_verified_by_reading_original_key(
    store: StateStore, registry: ToolRegistry
) -> None:
    registry.invoke(store, tool_reference("work_orders.create"), CREATE, request_key="create")
    reopened = StateStore(store.path)
    before = reopened.current_state()
    verified = registry.invoke(
        reopened,
        tool_reference("work_orders.get_by_idempotency_key"),
        {"idempotency_key": "create"},
    )
    assert verified.effect is None and verified.error is None
    effects = reopened.effects()
    assert len(effects) == 1 and effects[0].status == "complete"
    assert verified.observation == effects[0].result
    rows = json.loads(reopened.resolve_snapshot(before.state_hash))["tables"]["work_orders"]
    assert len(rows) == 1 and rows[0]["request_key"] == "create"
    assert reopened.current_state() == before


def test_replacement_path_replays_exactly(tmp_path: Path, registry: ToolRegistry) -> None:
    def run(store: StateStore) -> tuple[tuple[ToolResult, ...], str]:
        calls = (
            ("work_orders.create", CREATE, "create"),
            ("inventory.reserve", TRANSFER, "reserve"),
            ("notifications.send", SEND, "send"),
            ("inventory.reserve", TRANSFER, "reserve"),
            ("notifications.send", SEND, "send"),
        )
        results = tuple(
            registry.invoke(store, tool_reference(tool), args, request_key=key)
            for tool, args, key in calls
        )
        assert all(result.error is None for result in results)
        effects = store.effects()
        assert [row.status for row in effects] == [
            "complete",
            "complete",
            "complete",
            "duplicate",
            "duplicate",
        ]
        for effect in effects[:3]:
            assert store.state_diff(effect.state_before_hash, effect.state_after_hash)
        for effect in effects[3:]:
            assert effect.state_before_hash == effect.state_after_hash
        assert results[1].observation == results[3].observation
        assert results[2].observation == results[4].observation
        final = store.current_state().state_hash
        rows = json.loads(store.resolve_snapshot(final))["tables"]
        assert len(rows["work_orders"]) == len(rows["notifications"]) == 1
        assert rows["work_orders"][0]["work_order_id"] == stable_id("work_order", 19, "create")
        assert rows["notifications"][0]["message_id"] == stable_id("notification", 19, "send")
        assert rows["notifications"][0]["status"] == "delivered"
        assert rows["inventory"][0] == STOCK | {"available_count": 1, "reserved_count": 1}
        return results, final

    first = run(_new_store(tmp_path / "first.sqlite3", seed=19))
    second = run(_new_store(tmp_path / "second.sqlite3", seed=19))
    assert first == second


def test_concurrent_different_keys_cannot_exceed_approval(
    store: StateStore, registry: ToolRegistry
) -> None:
    def call(key: str) -> ToolResult:
        return registry.invoke(store, tool_reference("work_orders.create"), CREATE, request_key=key)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(call, ("first", "second")))
    errors = [result for result in results if result.error is not None]
    assert len(errors) == 1
    _error(errors[0], "precondition_failed")
    assert len(store.effects()) == 1


def test_partial_effect_surfaces_without_new_write(
    store: StateStore, registry: ToolRegistry
) -> None:
    def partial(db: sqlite3.Connection) -> EffectOutcome:
        db.execute("UPDATE cases SET notes = 'Started' WHERE case_id = 'mock-case-1'")
        return EffectOutcome("partial", {"step": "notes changed"})

    ref = tool_reference("work_orders.create")
    effect = store.apply_effect(ref, "create", CREATE, partial)
    before = store.current_state()
    result = registry.invoke(store, ref, CREATE, request_key="create")
    _error(result, "partial_effect")
    assert result.effect == effect
    assert store.effects() == (effect,) and store.current_state() == before


def test_write_postcondition_failure_rolls_back(store: StateStore, registry: ToolRegistry) -> None:
    with closing(sqlite3.connect(store.path, autocommit=True)) as db:
        db.execute("""CREATE TRIGGER alter_order AFTER INSERT ON work_orders
                      BEGIN UPDATE work_orders SET status = 'completed'; END""")
    initial = store.current_state()
    with pytest.raises(StateIntegrityError, match="postcondition failed"):
        registry.invoke(store, tool_reference("work_orders.create"), CREATE, request_key="create")
    assert store.current_state() == initial
    assert store.effects() == ()
    assert store.lookup_effect("create") is None


def test_invalid_write_result_rolls_back(
    store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    create = service_operations._create_work_order

    def bad_result(db: sqlite3.Connection, args: JsonObject, key: str) -> JsonObject:
        create(db, args, key)
        return {"unrecognized_field": "bad result"}

    monkeypatch.setattr(service_operations, "_create_work_order", bad_result)
    before = store.current_state()
    with pytest.raises(StateIntegrityError, match="result violates the contract"):
        build_service_registry().invoke(
            store, tool_reference("work_orders.create"), CREATE, request_key="create"
        )
    assert store.current_state() == before and not store.effects()
    assert store.lookup_effect("create") is None


def test_read_handler_cannot_write(store: StateStore, monkeypatch: pytest.MonkeyPatch) -> None:
    def accidental_write(db: sqlite3.Connection, args: JsonObject) -> JsonObject:
        db.execute("UPDATE cases SET status = 'closed' WHERE case_id = ?", (args["case_id"],))
        return args | {"status": "closed"}

    monkeypatch.setattr(service_operations, "_get_case", accidental_write)
    before = store.current_state()
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        build_service_registry().invoke(store, tool_reference("cases.get"), CASE)
    assert store.current_state() == before and not store.effects()


def test_startup_rejects_missing_service_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contracts = service_contracts(FIXTURES)
    monkeypatch.setattr(service_operations, "service_contracts", lambda _: contracts[:-1])
    manifest = Path("examples/feasibility/run.yaml").resolve()
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["scenario", "run", "--manifest", str(manifest)])
    assert result.exit_code == 2
    assert "contracts and handlers must match exactly" in result.stderr
    assert not (tmp_path / "artifacts").exists()


def test_startup_rejects_a_write_as_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contracts = service_contracts(FIXTURES)
    invalid = tuple(
        contract.model_copy(update={"verification_tool": tool_reference("work_orders.cancel")})
        if contract.tool_id == "work_orders.create"
        else contract
        for contract in contracts
    )
    monkeypatch.setattr(service_operations, "service_contracts", lambda _: invalid)
    manifest = Path("examples/feasibility/run.yaml").resolve()
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["scenario", "run", "--manifest", str(manifest)])
    assert result.exit_code == 2
    assert "verification path must be a registered read" in result.stderr
    assert not (tmp_path / "artifacts").exists()


def test_registry_resolves_only_exact_versions(registry: ToolRegistry, store: StateStore) -> None:
    with pytest.raises(InputError, match=r"unsupported service tool cases.get@3\.0\.0"):
        registry.invoke(store, tool_reference("cases.get", "3.0.0"), CASE)
    assert not store.effects()
