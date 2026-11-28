"""SQLite handlers for mock account, case, inventory, work-order, and delivery operations."""

import sqlite3
from pathlib import Path
from typing import cast

from pydantic import JsonValue

from toolswap.contracts import JsonObject
from toolswap.determinism import stable_id
from toolswap.service_contracts import MAX_SQLITE_INTEGER, service_contracts, tool_reference
from toolswap.service_tools import ToolError, ToolRegistry
from toolswap.state import StateIntegrityError, persisted_seed

FIXTURES = Path(__file__).with_name("fixtures")


def _row(
    db: sqlite3.Connection, query: str, parameters: tuple[JsonValue, ...], entity: str
) -> JsonObject:
    row = db.execute(query, parameters).fetchone()
    if row is None:
        raise ToolError("not_found", f"{entity} not found")
    return dict(row)


def _check_write(actual: JsonObject, expected: JsonObject, operation: str) -> JsonObject:
    if actual != expected:
        raise StateIntegrityError(f"postcondition failed for {operation}")
    return actual


def _get_account(db: sqlite3.Connection, args: JsonObject) -> JsonObject:
    account = _row(
        db,
        "SELECT account_id, status, verified FROM accounts WHERE account_id = ?",
        (args["account_id"],),
        "Account",
    )
    account["verified"] = bool(account["verified"])
    return account


def _verify_account(db: sqlite3.Connection, args: JsonObject) -> JsonObject:
    account = _get_account(db, args)
    return {"account_id": account["account_id"], "verified": account["verified"]}


def _get_case_details(db: sqlite3.Connection, args: JsonObject) -> JsonObject:
    case = _row(
        db,
        """SELECT case_id, account_id, issue, status, notes, replacement_approved
           FROM cases WHERE case_id = ?""",
        (args["case_id"],),
        "Case",
    )
    case["replacement_approved"] = bool(case["replacement_approved"])
    return case


def _get_case(db: sqlite3.Connection, args: JsonObject) -> JsonObject:
    case = _get_case_details(db, args)
    return {"case_id": case["case_id"], "status": case["status"]}


def _update_case(db: sqlite3.Connection, args: JsonObject, key: str) -> JsonObject:
    case = _get_case_details(db, args)
    if case["status"] != "open" or case["notes"] != args["expected_notes"]:
        raise ToolError("precondition_failed", "Case must be open and expected notes must match")
    if case["notes"] == args["notes"]:
        raise ToolError("precondition_failed", "Updated notes must differ from the current notes")
    db.execute("UPDATE cases SET notes = ? WHERE case_id = ?", (args["notes"], args["case_id"]))
    return _check_write(
        _get_case_details(db, args), case | {"notes": args["notes"]}, "cases.update"
    )


def _close_case(db: sqlite3.Connection, args: JsonObject, key: str) -> JsonObject:
    case = _get_case_details(db, args)
    if case["status"] != "open":
        raise ToolError("precondition_failed", "Case must be open")
    db.execute("UPDATE cases SET status = 'closed' WHERE case_id = ?", (args["case_id"],))
    return _check_write(_get_case_details(db, args), case | {"status": "closed"}, "cases.close")


def _get_stock(db: sqlite3.Connection, args: JsonObject) -> JsonObject:
    return _row(
        db,
        """SELECT sku, location, available_count, reserved_count
           FROM inventory WHERE sku = ? AND location = ?""",
        (args["sku"], args["location"]),
        "Inventory item at location",
    )


def _get_availability(db: sqlite3.Connection, args: JsonObject) -> JsonObject:
    stock = _get_stock(db, args)
    return {field: stock[field] for field in ("sku", "location", "available_count")}


def _get_reservation(db: sqlite3.Connection, args: JsonObject) -> JsonObject:
    stock = _get_stock(db, args)
    return {field: stock[field] for field in ("sku", "location", "reserved_count")}


def _transfer_stock(db: sqlite3.Connection, args: JsonObject, *, reserve: bool) -> JsonObject:
    stock = _get_stock(db, args)
    quantity = int(cast(int, args["quantity"]))
    available = cast(int, stock["available_count"])
    reserved = cast(int, stock["reserved_count"])
    if reserve:
        available -= quantity
        reserved += quantity
    else:
        available += quantity
        reserved -= quantity
    if not (0 <= available <= MAX_SQLITE_INTEGER and 0 <= reserved <= MAX_SQLITE_INTEGER):
        raise ToolError(
            "precondition_failed", "Inventory counts cannot cover the requested transfer"
        )
    db.execute(
        """UPDATE inventory SET available_count = ?, reserved_count = ?
           WHERE sku = ? AND location = ?""",
        (available, reserved, args["sku"], args["location"]),
    )
    return _check_write(
        _get_stock(db, args),
        stock | {"available_count": available, "reserved_count": reserved},
        "inventory.reserve" if reserve else "inventory.release",
    )


def _reserve_stock(db: sqlite3.Connection, args: JsonObject, key: str) -> JsonObject:
    return _transfer_stock(db, args, reserve=True)


def _release_stock(db: sqlite3.Connection, args: JsonObject, key: str) -> JsonObject:
    return _transfer_stock(db, args, reserve=False)


def _get_work_order(db: sqlite3.Connection, args: JsonObject) -> JsonObject:
    return _row(
        db,
        "SELECT work_order_id, case_id, action, status FROM work_orders WHERE work_order_id = ?",
        (args["work_order_id"],),
        "Work order",
    )


def _get_work_order_by_key(db: sqlite3.Connection, args: JsonObject) -> JsonObject:
    return _row(
        db,
        "SELECT work_order_id, case_id, action, status FROM work_orders WHERE request_key = ?",
        (args["idempotency_key"],),
        "Work order",
    )


def _create_work_order(db: sqlite3.Connection, args: JsonObject, key: str) -> JsonObject:
    case = _get_case_details(db, args)
    if case["status"] != "open" or not case["replacement_approved"]:
        raise ToolError("precondition_failed", "Replacement requires an open, approved case")
    already_created = db.execute(
        """SELECT 1 FROM work_orders
           WHERE case_id = ? AND action = 'replace' AND status != 'cancelled' LIMIT 1""",
        (args["case_id"],),
    ).fetchone()
    if already_created is not None:
        raise ToolError("precondition_failed", "Case already has a non-cancelled replacement")
    order_id = stable_id("work_order", persisted_seed(db), key)
    db.execute(
        """INSERT INTO work_orders (work_order_id, case_id, action, status, request_key)
           VALUES (?, ?, ?, 'created', ?)""",
        (order_id, args["case_id"], args["action"], key),
    )
    expected: JsonObject = {
        "work_order_id": order_id,
        "case_id": args["case_id"],
        "action": args["action"],
        "status": "created",
    }
    return _check_write(
        _get_work_order_by_key(db, {"idempotency_key": key}), expected, "work_orders.create"
    )


def _cancel_work_order(db: sqlite3.Connection, args: JsonObject, key: str) -> JsonObject:
    order = _get_work_order(db, args)
    if order["status"] != "created":
        raise ToolError("precondition_failed", "Work order must have created status")
    db.execute(
        "UPDATE work_orders SET status = 'cancelled' WHERE work_order_id = ?",
        (args["work_order_id"],),
    )
    return _check_write(
        _get_work_order(db, args), order | {"status": "cancelled"}, "work_orders.cancel"
    )


def _get_delivery_status(db: sqlite3.Connection, args: JsonObject) -> JsonObject:
    return _row(
        db,
        "SELECT message_id, case_id, channel, status FROM notifications WHERE request_key = ?",
        (args["idempotency_key"],),
        "Notification",
    )


def _send_notification(db: sqlite3.Connection, args: JsonObject, key: str) -> JsonObject:
    _get_case_details(db, args)
    message_id = stable_id("notification", persisted_seed(db), key)
    db.execute(
        """INSERT INTO notifications (message_id, case_id, channel, status, request_key)
           VALUES (?, ?, ?, 'delivered', ?)""",
        (message_id, args["case_id"], args["channel"], key),
    )
    expected: JsonObject = {
        "message_id": message_id,
        "case_id": args["case_id"],
        "channel": args["channel"],
        "status": "delivered",
    }
    return _check_write(
        _get_delivery_status(db, {"idempotency_key": key}), expected, "notifications.send"
    )


def build_service_registry(fixtures: Path = FIXTURES) -> ToolRegistry:
    return ToolRegistry(
        service_contracts(fixtures),
        reads={
            tool_reference("accounts.get"): _get_account,
            tool_reference("accounts.verify"): _verify_account,
            tool_reference("cases.get"): _get_case,
            tool_reference("cases.get", "2.0.0"): _get_case_details,
            tool_reference("inventory.get_availability"): _get_availability,
            tool_reference("inventory.get_reservation_state"): _get_reservation,
            tool_reference("work_orders.get"): _get_work_order,
            tool_reference("work_orders.get_by_idempotency_key"): _get_work_order_by_key,
            tool_reference("notifications.get_delivery_status"): _get_delivery_status,
        },
        writes={
            tool_reference("cases.update"): _update_case,
            tool_reference("cases.close"): _close_case,
            tool_reference("inventory.reserve"): _reserve_stock,
            tool_reference("inventory.release"): _release_stock,
            tool_reference("work_orders.create"): _create_work_order,
            tool_reference("work_orders.cancel"): _cancel_work_order,
            tool_reference("notifications.send"): _send_notification,
        },
    )
