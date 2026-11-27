"""Canonical business state and deterministic row-level change evidence."""

import sqlite3
from typing import Final

from pydantic import JsonValue, ValidationError

from toolswap.config import InputError
from toolswap.contracts import SCHEMA_VERSION, JsonObject, VersionedRecord
from toolswap.determinism import canonical_json

type StateRow = dict[str, str | int]

# Parents precede children for seeding. The primary keys also define snapshot row order.
TABLE_KEYS: Final = {
    "accounts": ("account_id",),
    "cases": ("case_id",),
    "inventory": ("sku", "location"),
    "work_orders": ("work_order_id",),
    "notifications": ("message_id",),
}


class Snapshot(VersionedRecord):
    tables: dict[str, tuple[StateRow, ...]]


def decode_snapshot(payload: bytes) -> Snapshot:
    try:
        snapshot = Snapshot.model_validate_json(payload)
    except ValidationError as exc:
        raise InputError("invalid service-operations snapshot") from exc
    if snapshot.tables.keys() != TABLE_KEYS.keys():
        raise InputError("snapshot must contain exactly the service-operations tables")
    if canonical_json(snapshot.model_dump(mode="json")) != payload:
        raise InputError("snapshot bytes must be canonical JSON")
    return snapshot


def capture(db: sqlite3.Connection) -> bytes:
    tables: JsonObject = {}
    for table, key in TABLE_KEYS.items():
        rows: list[JsonValue] = [
            dict(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY {', '.join(key)}")
        ]
        tables[table] = rows
    return canonical_json({"schema_version": SCHEMA_VERSION, "tables": tables})


def seed_business_state(db: sqlite3.Connection, snapshot: Snapshot) -> None:
    for table in TABLE_KEYS:
        columns = tuple(row["name"] for row in db.execute(f"PRAGMA table_info({table})"))
        expected = set(columns)
        placeholders = ", ".join("?" for _ in columns)
        sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
        for row in snapshot.tables[table]:
            if row.keys() != expected:
                raise InputError(f"snapshot row has invalid columns for {table}")
            try:
                db.execute(sql, tuple(row[column] for column in columns))
            except (sqlite3.IntegrityError, OverflowError) as exc:
                raise InputError(f"snapshot row violates constraints for {table}") from exc


def _key(row: StateRow, columns: tuple[str, ...]) -> tuple[str | int, ...]:
    return tuple(row[column] for column in columns)


def diff_snapshots(before: Snapshot, after: Snapshot) -> list[JsonObject]:
    changes: list[JsonObject] = []
    for table, columns in TABLE_KEYS.items():
        earlier = {_key(row, columns): row for row in before.tables[table]}
        later = {_key(row, columns): row for row in after.tables[table]}
        for key in sorted(earlier.keys() | later.keys()):
            old, new = earlier.get(key), later.get(key)
            location: JsonObject = {"table": table, "key": dict(zip(columns, key, strict=True))}
            if old is None:
                assert new is not None
                added: JsonObject = dict(new)
                changes.append(location | {"operation": "add", "after": added})
            elif new is None:
                removed: JsonObject = dict(old)
                changes.append(location | {"operation": "remove", "before": removed})
            else:
                for field in sorted(old):
                    if old[field] != new[field]:
                        changes.append(
                            location
                            | {
                                "operation": "replace",
                                "field": field,
                                "before": old[field],
                                "after": new[field],
                            }
                        )
    return changes
