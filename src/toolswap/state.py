"""Run-local SQLite state, snapshot evidence, and transactional effect recording."""

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from os import link
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from pydantic import TypeAdapter

from toolswap.config import InputError
from toolswap.contracts import (
    SCHEMA_VERSION,
    EffectRecord,
    JsonObject,
    Reference,
    Scenario,
    Seed,
    StateReference,
)
from toolswap.determinism import canonical_json, content_hash, stable_id
from toolswap.state_snapshots import capture, decode_snapshot, diff_snapshots, seed_business_state

MIGRATION = Path(__file__).with_name("migrations") / "001_service_ops.sql"
DB_VERSION = 1
SEED = TypeAdapter(Seed)


class StateIntegrityError(OSError):
    """Stored state evidence or the database schema could not be verified."""


class IdempotencyConflict(ValueError):
    """A run's request key is already bound to a different request."""


@dataclass(frozen=True)
class EffectOutcome:
    """The underlying outcome reported by a trusted mutation function."""

    status: Literal["not_run", "partial", "complete"]
    result: JsonObject


def _connect(path: Path, mode: Literal["ro", "rw", "rwc"]) -> sqlite3.Connection:
    db = sqlite3.connect(f"{path.absolute().as_uri()}?mode={mode}", uri=True, autocommit=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


@contextmanager
def _transaction(db: sqlite3.Connection, begin: str) -> Iterator[None]:
    db.execute(begin)
    try:
        yield
        db.execute("COMMIT")
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise


def _resolve_snapshot(db: sqlite3.Connection, state_hash: str) -> bytes:
    row = db.execute(
        "SELECT payload FROM state_snapshots WHERE state_hash = ?", (state_hash,)
    ).fetchone()
    if row is None:
        raise StateIntegrityError(f"missing snapshot {state_hash}")
    payload = bytes(row["payload"])
    if content_hash(payload) != state_hash:
        raise StateIntegrityError(f"snapshot checksum mismatch for {state_hash}")
    return payload


def _store_snapshot(db: sqlite3.Connection, payload: bytes) -> str:
    state_hash = content_hash(payload)
    db.execute(
        "INSERT INTO state_snapshots VALUES (?, ?) ON CONFLICT(state_hash) DO NOTHING",
        (state_hash, payload),
    )
    if _resolve_snapshot(db, state_hash) != payload:
        raise StateIntegrityError(f"stored snapshot differs for {state_hash}")
    return state_hash


def _effect(row: sqlite3.Row) -> EffectRecord:
    return EffectRecord.model_validate(
        {
            "schema_version": SCHEMA_VERSION,
            "effect_id": row["effect_id"],
            "tool": {"id": row["tool_id"], "version": row["tool_version"]},
            "request_key": row["request_key"],
            "status": row["status"],
            "state_before_hash": row["state_before_hash"],
            "state_after_hash": row["state_after_hash"],
            "result": json.loads(row["result"]),
        }
    )


def _registered(db: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    row: sqlite3.Row | None = db.execute(
        """SELECT effect_ledger.*, idempotency_keys.fingerprint
           FROM idempotency_keys JOIN effect_ledger USING (effect_id)
           WHERE idempotency_keys.request_key = ?""",
        (key,),
    ).fetchone()
    return row


def _append_effect(
    db: sqlite3.Connection,
    tool: Reference,
    request_key: str,
    outcome: EffectOutcome,
    before: str,
    after: str,
    *,
    duplicate: bool,
) -> EffectRecord:
    state = db.execute("SELECT * FROM run_state WHERE singleton = 1").fetchone()
    if state is None:
        raise StateIntegrityError("database is missing its initial run state")
    sequence = db.execute("SELECT COALESCE(MAX(sequence), 0) + 1 FROM effect_ledger").fetchone()[0]
    record = EffectRecord(
        schema_version=SCHEMA_VERSION,
        effect_id=stable_id(
            "effect",
            int(state["seed"]),
            state["scenario_id"],
            state["scenario_version"],
            state["initial_snapshot"],
            sequence,
        ),
        tool=tool,
        request_key=request_key,
        status="duplicate" if duplicate else outcome.status,
        state_before_hash=before,
        state_after_hash=after,
        result=outcome.result,
    )
    db.execute(
        "INSERT INTO effect_ledger VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            sequence,
            record.effect_id,
            tool.id,
            tool.version,
            request_key,
            record.status,
            before,
            after,
            canonical_json(record.result),
        ),
    )
    return record


class StateStore:
    """One SQLite database per run. Each operation owns its connection and transaction."""

    def __init__(self, path: Path):
        self.path = path.absolute()

    @classmethod
    def reset(
        cls, path: Path, scenario: Scenario, snapshot: bytes, *, seed: int | None = None
    ) -> "StateStore":
        """Create a fresh run database in an existing directory without replacing any file."""
        if content_hash(snapshot) != scenario.initial_snapshot:
            raise InputError("initial snapshot does not match the scenario checksum")
        decoded = decode_snapshot(snapshot)
        run_seed = SEED.validate_python(scenario.seed if seed is None else seed, strict=True)
        path = path.absolute()
        with TemporaryDirectory(dir=path.parent) as scratch:
            staged = Path(scratch) / "state.sqlite3"
            with closing(_connect(staged, "rwc")) as db:
                db.execute("PRAGMA page_size = 4096")
                migration = MIGRATION.read_text(encoding="utf-8")
                db.executescript(
                    f"BEGIN IMMEDIATE;\n{migration}\nPRAGMA user_version = {DB_VERSION};"
                )
                seed_business_state(db, decoded)
                if capture(db) != snapshot:
                    raise InputError("seeded rows do not reproduce the canonical initial snapshot")
                initial = _store_snapshot(db, snapshot)
                db.execute(
                    "INSERT INTO run_state VALUES (1, ?, ?, ?, ?)",
                    (scenario.scenario_id, scenario.version, initial, str(run_seed)),
                )
                db.execute("COMMIT")
            link(staged, path)
        return cls(path)

    @contextmanager
    def _open(self, *, writable: bool = False) -> Iterator[sqlite3.Connection]:
        with closing(_connect(self.path, "rw" if writable else "ro")) as db:
            if db.execute("PRAGMA user_version").fetchone()[0] != DB_VERSION:
                raise StateIntegrityError("unsupported SQLite schema version")
            yield db

    def current_state(self) -> StateReference:
        with self._open() as db, _transaction(db, "BEGIN"):
            return StateReference(
                schema_version=SCHEMA_VERSION, state_hash=content_hash(capture(db))
            )

    def resolve_snapshot(self, state_hash: str) -> bytes:
        with self._open() as db:
            return _resolve_snapshot(db, state_hash)

    def snapshot_artifact(self, state_hash: str) -> tuple[str, bytes]:
        """Return the relative run artifact path and its verified payload."""
        payload = self.resolve_snapshot(state_hash)
        return f"states/{state_hash.removeprefix('sha256:')}.json", payload

    def state_diff(self, before_hash: str, after_hash: str) -> list[JsonObject]:
        with self._open() as db, _transaction(db, "BEGIN"):
            before = decode_snapshot(_resolve_snapshot(db, before_hash))
            after = decode_snapshot(_resolve_snapshot(db, after_hash))
            return diff_snapshots(before, after)

    def lookup_effect(self, request_key: str) -> EffectRecord | None:
        """Read the latest underlying outcome for a key, excluding duplicate attempts."""
        with self._open() as db:
            row = _registered(db, request_key)
            return None if row is None else _effect(row)

    def effects(self) -> tuple[EffectRecord, ...]:
        with self._open() as db:
            return tuple(
                _effect(row) for row in db.execute("SELECT * FROM effect_ledger ORDER BY sequence")
            )

    def apply_effect(
        self,
        tool: Reference,
        request_key: str,
        arguments: JsonObject,
        mutate: Callable[[sqlite3.Connection], EffectOutcome],
    ) -> EffectRecord:
        """Apply an authorized, validated request using trusted business SQL in our transaction."""
        if not request_key:
            raise InputError("effect writes require a nonempty request key")
        fingerprint = content_hash(
            canonical_json({"tool": tool.model_dump(mode="json"), "arguments": arguments})
        )
        with self._open(writable=True) as db, _transaction(db, "BEGIN IMMEDIATE"):
            prior = _registered(db, request_key)
            if prior is not None and prior["fingerprint"] != fingerprint:
                raise IdempotencyConflict(
                    "request key is already bound to a different tool or arguments"
                )
            if prior is not None and prior["status"] == "partial":
                return _effect(prior)

            before = _store_snapshot(db, capture(db))
            if prior is not None and prior["status"] == "complete":
                return _append_effect(
                    db,
                    tool,
                    request_key,
                    EffectOutcome("complete", _effect(prior).result),
                    before,
                    before,
                    duplicate=True,
                )

            outcome = mutate(db)
            if outcome.status not in ("not_run", "partial", "complete"):
                raise ValueError("mutation returned an invalid effect status")
            after = _store_snapshot(db, capture(db))
            record = _append_effect(db, tool, request_key, outcome, before, after, duplicate=False)
            db.execute(
                """INSERT INTO idempotency_keys VALUES (?, ?, ?)
                   ON CONFLICT(request_key) DO UPDATE SET effect_id = excluded.effect_id""",
                (request_key, fingerprint, record.effect_id),
            )
            return record
