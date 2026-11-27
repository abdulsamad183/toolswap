import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier

import pytest

from toolswap.config import InputError, parse_yaml_record
from toolswap.contracts import JsonObject, Reference, Scenario
from toolswap.determinism import canonical_json, content_hash
from toolswap.scenario import FIXTURES
from toolswap.state import EffectOutcome, IdempotencyConflict, StateIntegrityError, StateStore

WORK_ORDERS_CREATE = Reference(id="work_orders.create", version="1.0.0")
REQUEST_KEY = "request-1"
ARGUMENTS: JsonObject = {"case_id": "mock-case-1", "action": "replace"}
ORDER: JsonObject = {
    "work_order_id": "mock-order-1",
    "case_id": "mock-case-1",
    "action": "replace",
    "status": "created",
    "request_key": REQUEST_KEY,
}


@pytest.fixture
def initial() -> tuple[Scenario, bytes]:
    scenario = parse_yaml_record(FIXTURES / "state_safety.yaml", Scenario)
    return scenario, canonical_json(
        json.loads((FIXTURES / "state_safety_snapshot.json").read_bytes())
    )


@pytest.fixture
def store(tmp_path: Path, initial: tuple[Scenario, bytes]) -> StateStore:
    return StateStore.reset(tmp_path / "state.sqlite3", *initial)


def _create_order(db: sqlite3.Connection) -> EffectOutcome:
    db.execute(
        """INSERT INTO work_orders VALUES
           (:work_order_id, :case_id, :action, :status, :request_key)""",
        ORDER,
    )
    return EffectOutcome("complete", {"work_order_id": ORDER["work_order_id"]})


def _must_not_run(db: sqlite3.Connection) -> EffectOutcome:
    pytest.fail("duplicate or conflicting request was dispatched")


def test_fresh_migrations_and_resets_are_exact(
    tmp_path: Path, initial: tuple[Scenario, bytes]
) -> None:
    scenario, snapshot = initial
    first = StateStore.reset(tmp_path / "one.sqlite3", *initial)
    second = StateStore.reset(tmp_path / "two.sqlite3", *initial)
    assert first.path.read_bytes() == second.path.read_bytes()
    assert first.current_state() == second.current_state()
    assert first.current_state().state_hash == scenario.initial_snapshot == content_hash(snapshot)
    assert first.resolve_snapshot(scenario.initial_snapshot) == snapshot
    with closing(sqlite3.connect(first.path)) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (1,)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    first_effect = first.apply_effect(WORK_ORDERS_CREATE, REQUEST_KEY, ARGUMENTS, _create_order)
    fresh = StateStore.reset(tmp_path / "three.sqlite3", *initial)
    assert first.current_state().state_hash != scenario.initial_snapshot
    assert fresh.path.read_bytes() == second.path.read_bytes()
    assert fresh.lookup_effect(REQUEST_KEY) is None
    assert (
        fresh.apply_effect(WORK_ORDERS_CREATE, REQUEST_KEY, ARGUMENTS, _create_order)
        == first_effect
    )
    existing = first.path.read_bytes()
    with pytest.raises(FileExistsError):
        StateStore.reset(first.path, *initial)
    assert first.path.read_bytes() == existing


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE cases SET account_id = 'unknown'",
        "UPDATE inventory SET available_count = -1",
        "UPDATE inventory SET reserved_count = -1",
        "INSERT INTO inventory VALUES ('mock-part-1', 'north', 0, 0)",
        "UPDATE accounts SET verified = 2",
        "INSERT INTO work_orders VALUES "
        "('duplicate-id', 'mock-case-1', 'replace', 'created', 'request-1')",
        "INSERT INTO notifications VALUES ('message-1', 'unknown', 'sms', 'sent', 'message-key')",
    ],
)
def test_database_enforces_domain_constraints(store: StateStore, statement: str) -> None:
    before = store.current_state()

    def invalid(db: sqlite3.Connection) -> EffectOutcome:
        result = _create_order(db)
        db.execute(statement)
        return result

    with pytest.raises(sqlite3.IntegrityError):
        store.apply_effect(WORK_ORDERS_CREATE, REQUEST_KEY, ARGUMENTS, invalid)
    assert store.current_state() == before
    assert store.effects() == ()
    assert store.lookup_effect(REQUEST_KEY) is None


def test_reset_requires_declared_canonical_snapshot(
    tmp_path: Path, initial: tuple[Scenario, bytes]
) -> None:
    scenario, snapshot = initial
    database = tmp_path / "state.sqlite3"
    changed = snapshot.replace(b"Replacement approved", b"Review pending")
    with pytest.raises(InputError, match="checksum"):
        StateStore.reset(database, scenario, changed)
    noncanonical = snapshot + b"\n"
    with pytest.raises(InputError, match="canonical JSON"):
        StateStore.reset(
            database,
            scenario.model_copy(update={"initial_snapshot": content_hash(noncanonical)}),
            noncanonical,
        )
    invalid_state = json.loads(snapshot)
    invalid_state["tables"]["cases"][0]["account_id"] = "unknown-account"
    invalid = canonical_json(invalid_state)
    with pytest.raises(InputError, match="violates constraints for cases"):
        StateStore.reset(
            database,
            scenario.model_copy(update={"initial_snapshot": content_hash(invalid)}),
            invalid,
        )
    assert list(tmp_path.iterdir()) == []

    revised = scenario.model_copy(
        update={"version": "1.1.0", "initial_snapshot": content_hash(changed)}
    )
    assert (
        StateStore.reset(database, revised, changed).current_state().state_hash
        != scenario.initial_snapshot
    )


def test_read_does_not_change_state_or_effects(store: StateStore) -> None:
    original_db = store.path.read_bytes()
    before = store.current_state().state_hash
    path, payload = store.snapshot_artifact(before)
    assert path == f"states/{before.removeprefix('sha256:')}.json"
    assert content_hash(payload) == before
    assert store.state_diff(before, store.current_state().state_hash) == []
    assert store.effects() == ()
    assert store.lookup_effect(REQUEST_KEY) is None
    assert store.path.read_bytes() == original_db


def test_write_has_resolvable_minimal_state_diff(store: StateStore) -> None:
    def change_state(db: sqlite3.Connection) -> EffectOutcome:
        db.execute("UPDATE cases SET notes = 'Assigned' WHERE case_id = 'mock-case-1'")
        db.execute("DELETE FROM inventory WHERE sku = 'mock-part-1' AND location = 'south'")
        return _create_order(db)

    effect = store.apply_effect(WORK_ORDERS_CREATE, REQUEST_KEY, ARGUMENTS, change_state)
    before, after = effect.state_before_hash, effect.state_after_hash
    assert before != after == store.current_state().state_hash
    assert all(content_hash(store.resolve_snapshot(key)) == key for key in (before, after))
    assert store.state_diff(before, after) == [
        {
            "table": "cases",
            "key": {"case_id": "mock-case-1"},
            "operation": "replace",
            "field": "notes",
            "before": "Replacement approved",
            "after": "Assigned",
        },
        {
            "table": "inventory",
            "key": {"sku": "mock-part-1", "location": "south"},
            "operation": "remove",
            "before": {
                "sku": "mock-part-1",
                "location": "south",
                "available_count": 0,
                "reserved_count": 0,
            },
        },
        {
            "table": "work_orders",
            "key": {"work_order_id": "mock-order-1"},
            "operation": "add",
            "after": ORDER,
        },
    ]
    assert store.state_diff(before, after) == StateStore(store.path).state_diff(before, after)


def test_snapshot_corruption_is_detected_on_read(store: StateStore) -> None:
    state_hash = store.current_state().state_hash
    with closing(sqlite3.connect(store.path, autocommit=True)) as db:
        db.execute("UPDATE state_snapshots SET payload = ?", (b"tampered",))
    with pytest.raises(StateIntegrityError, match="checksum mismatch"):
        store.snapshot_artifact(state_hash)
    with pytest.raises(StateIntegrityError, match="checksum mismatch"):
        store.apply_effect(WORK_ORDERS_CREATE, REQUEST_KEY, ARGUMENTS, _must_not_run)
    assert store.effects() == ()


def test_not_run_then_complete_then_duplicate(store: StateStore) -> None:
    def not_run(db: sqlite3.Connection) -> EffectOutcome:
        return EffectOutcome("not_run", {"reason": "write could not start"})

    skipped = store.apply_effect(WORK_ORDERS_CREATE, REQUEST_KEY, ARGUMENTS, not_run)
    assert skipped.status == "not_run"
    assert skipped.state_before_hash == skipped.state_after_hash
    assert store.lookup_effect(REQUEST_KEY) == skipped

    committed = store.apply_effect(WORK_ORDERS_CREATE, REQUEST_KEY, ARGUMENTS, _create_order)
    # Use a fresh handle and reverse argument key order to exercise lookup and canonical matching.
    reopened = StateStore(store.path)
    assert reopened.lookup_effect(REQUEST_KEY) == committed
    copied = reopened.apply_effect(
        WORK_ORDERS_CREATE, REQUEST_KEY, dict(reversed(ARGUMENTS.items())), _must_not_run
    )
    assert committed.status == "complete"
    assert copied.status == "duplicate"
    assert copied.result == committed.result
    assert copied.effect_id != committed.effect_id
    assert copied.state_before_hash == copied.state_after_hash == committed.state_after_hash
    assert reopened.lookup_effect(REQUEST_KEY) == committed
    assert reopened.effects() == (skipped, committed, copied)
    rows = json.loads(reopened.resolve_snapshot(copied.state_after_hash))["tables"]["work_orders"]
    assert rows == [ORDER]


def test_partial_effect_blocks_redispatch(store: StateStore) -> None:
    def partial(db: sqlite3.Connection) -> EffectOutcome:
        db.execute("UPDATE cases SET notes = 'Work begun' WHERE case_id = 'mock-case-1'")
        return EffectOutcome("partial", {"reason": "interrupted before creating the order"})

    effect = store.apply_effect(WORK_ORDERS_CREATE, REQUEST_KEY, ARGUMENTS, partial)
    assert effect.status == "partial"
    assert effect.state_before_hash != effect.state_after_hash
    assert store.lookup_effect(REQUEST_KEY) == effect
    assert store.apply_effect(WORK_ORDERS_CREATE, REQUEST_KEY, ARGUMENTS, _must_not_run) == effect
    assert store.effects() == (effect,)


def test_idempotency_conflicts_fail_before_mutation(store: StateStore) -> None:
    original = store.apply_effect(WORK_ORDERS_CREATE, REQUEST_KEY, ARGUMENTS, _create_order)
    before = store.path.read_bytes()
    different_requests = (
        (WORK_ORDERS_CREATE, ARGUMENTS | {"case_id": "mock-case-2"}),
        (Reference(id=WORK_ORDERS_CREATE.id, version="1.1.0"), ARGUMENTS),
    )
    for tool, args in different_requests:
        with pytest.raises(IdempotencyConflict, match="different tool or arguments"):
            store.apply_effect(tool, REQUEST_KEY, args, _must_not_run)
    with pytest.raises(InputError, match="nonempty request key"):
        store.apply_effect(WORK_ORDERS_CREATE, "", ARGUMENTS, _must_not_run)
    assert store.path.read_bytes() == before
    assert store.lookup_effect(REQUEST_KEY) == original


def test_lost_response_can_be_verified_after_reopening(store: StateStore) -> None:
    store.apply_effect(WORK_ORDERS_CREATE, REQUEST_KEY, ARGUMENTS, _create_order)
    reopened = StateStore(store.path)
    effect = reopened.lookup_effect(REQUEST_KEY)
    assert effect is not None and effect.status == "complete"
    assert reopened.effects() == (effect,)
    assert effect.state_after_hash == reopened.current_state().state_hash
    rows = json.loads(reopened.resolve_snapshot(effect.state_after_hash))["tables"]["work_orders"]
    assert rows == [ORDER]


def test_late_failure_rolls_back_state_evidence_and_key(store: StateStore) -> None:
    before = store.current_state().state_hash
    with closing(sqlite3.connect(store.path, autocommit=True)) as db:
        db.execute(
            """CREATE TRIGGER reject_registration BEFORE INSERT ON idempotency_keys
               BEGIN SELECT RAISE(ABORT, 'simulated late failure'); END"""
        )
    with pytest.raises(sqlite3.IntegrityError, match="simulated late failure"):
        store.apply_effect(WORK_ORDERS_CREATE, REQUEST_KEY, ARGUMENTS, _create_order)
    assert store.current_state().state_hash == before
    assert store.effects() == ()
    assert store.lookup_effect(REQUEST_KEY) is None
    with closing(sqlite3.connect(store.path, autocommit=True)) as db:
        assert db.execute("SELECT state_hash FROM state_snapshots").fetchall() == [(before,)]
        db.execute("DROP TRIGGER reject_registration")
    assert (
        store.apply_effect(WORK_ORDERS_CREATE, REQUEST_KEY, ARGUMENTS, _create_order).status
        == "complete"
    )


@pytest.mark.parametrize("status", ["not_run", "partial"])
def test_effect_status_must_agree_with_business_state(store: StateStore, status: str) -> None:
    def inconsistent(db: sqlite3.Connection) -> EffectOutcome:
        if status == "not_run":
            _create_order(db)
            return EffectOutcome("not_run", {})
        return EffectOutcome("partial", {})

    before = store.current_state()
    with pytest.raises(sqlite3.IntegrityError):
        store.apply_effect(WORK_ORDERS_CREATE, REQUEST_KEY, ARGUMENTS, inconsistent)
    assert store.current_state() == before
    assert store.effects() == ()


def test_simultaneous_requests_apply_once(store: StateStore) -> None:
    start = Barrier(2, timeout=5)
    dispatched: list[None] = []

    def call() -> str:
        def mutate(db: sqlite3.Connection) -> EffectOutcome:
            dispatched.append(None)
            return _create_order(db)

        start.wait()
        return (
            StateStore(store.path)
            .apply_effect(WORK_ORDERS_CREATE, REQUEST_KEY, ARGUMENTS, mutate)
            .status
        )

    with ThreadPoolExecutor(max_workers=2) as worker:
        one, two = (worker.submit(call) for _ in range(2))
        assert sorted((one.result(timeout=10), two.result(timeout=10))) == ["complete", "duplicate"]
    assert dispatched == [None]
    written, duplicate = store.effects()
    assert store.lookup_effect(REQUEST_KEY) == written
    assert duplicate.state_after_hash == written.state_after_hash
    rows = json.loads(store.resolve_snapshot(written.state_after_hash))["tables"]["work_orders"]
    assert rows == [ORDER]
