"""Versioned contracts for the SQLite service-operations tools."""

from pathlib import Path
from typing import Literal

from toolswap.config import InputError, parse_yaml_record
from toolswap.contracts import SCHEMA_VERSION, JsonObject, Reference, ToolContract, ToolFailure

MAX_SQLITE_INTEGER = 2**63 - 1
TEXT: JsonObject = {"type": "string"}
IDENTIFIER: JsonObject = {"type": "string", "minLength": 1}
BOOLEAN: JsonObject = {"type": "boolean"}
STOCK_COUNT: JsonObject = {"type": "integer", "minimum": 0, "maximum": MAX_SQLITE_INTEGER}
QUANTITY: JsonObject = STOCK_COUNT | {"minimum": 1}
CHANNEL: JsonObject = {"type": "string", "enum": ["email", "sms"]}
CASE_STATUS: JsonObject = {"type": "string", "enum": ["open", "closed"]}
REPLACEMENT: JsonObject = {"type": "string", "const": "replace"}


def tool_reference(tool_id: str, version: str = "1.0.0") -> Reference:
    return Reference(id=tool_id, version=version)


def _object(**fields: JsonObject) -> JsonObject:
    properties: JsonObject = {name: schema for name, schema in fields.items()}
    return {
        "type": "object",
        "properties": properties,
        "required": list(fields),
        "additionalProperties": False,
    }


def _contract(
    ref: Reference,
    inputs: JsonObject,
    outputs: JsonObject,
    *,
    authority: str,
    precondition: str,
    postcondition: str,
    side_effect: Literal["none", "reversible", "consequential"] = "none",
    verify: Reference | None = None,
) -> ToolContract:
    writes = side_effect != "none"
    codes: tuple[str, ...] = ("invalid_arguments", "not_found", "timeout")
    if writes:
        codes += ("precondition_failed", "idempotency_conflict", "partial_effect")
    return ToolContract(
        schema_version=SCHEMA_VERSION,
        tool_id=ref.id,
        version=ref.version,
        input_schema=inputs,
        output_schema=outputs,
        error_schema=_object(code={"type": "string", "enum": list(codes)}, message=IDENTIFIER),
        side_effect=side_effect,
        idempotency="key_required" if writes else "natural",
        authority=(authority,),
        requires_confirmation=False,
        preconditions=(precondition,),
        postconditions=(postcondition,),
        failure_modes=tuple(ToolFailure(code=code, retryable=code == "timeout") for code in codes),
        verification_tool=verify,
    )


def service_contracts(fixtures: Path) -> tuple[ToolContract, ...]:
    try:
        legacy_case = parse_yaml_record(fixtures / "cases_get.yaml", ToolContract)
    except FileNotFoundError as exc:
        raise InputError("missing cases.get@1.0.0 contract") from exc

    account_id = _object(account_id=IDENTIFIER)
    case_id = _object(case_id=IDENTIFIER)
    stock_key = _object(sku=IDENTIFIER, location=IDENTIFIER)
    stock_transfer = _object(sku=IDENTIFIER, location=IDENTIFIER, quantity=QUANTITY)
    stock_record = _object(
        sku=IDENTIFIER, location=IDENTIFIER, available_count=STOCK_COUNT, reserved_count=STOCK_COUNT
    )
    work_order_id = _object(work_order_id=IDENTIFIER)
    request_lookup = _object(idempotency_key=IDENTIFIER)
    order_record = _object(
        work_order_id=IDENTIFIER,
        case_id=IDENTIFIER,
        action=IDENTIFIER,
        status={"type": "string", "enum": ["created", "completed", "cancelled"]},
    )
    case_record = _object(
        case_id=IDENTIFIER,
        account_id=IDENTIFIER,
        issue=IDENTIFIER,
        status=CASE_STATUS,
        notes=TEXT,
        replacement_approved=BOOLEAN,
    )
    delivery_record = _object(
        message_id=IDENTIFIER,
        case_id=IDENTIFIER,
        channel=CHANNEL,
        status={"type": "string", "enum": ["queued", "sent", "delivered", "failed"]},
    )
    case_details = tool_reference("cases.get", "2.0.0")
    reservation = tool_reference("inventory.get_reservation_state")
    order = tool_reference("work_orders.get")
    order_by_key = tool_reference("work_orders.get_by_idempotency_key")
    delivery = tool_reference("notifications.get_delivery_status")
    return (
        legacy_case,
        _contract(
            tool_reference("accounts.get"),
            account_id,
            _object(
                account_id=IDENTIFIER,
                status={"type": "string", "enum": ["active", "suspended"]},
                verified=BOOLEAN,
            ),
            authority="account.read",
            precondition="Account exists",
            postcondition="Result matches the stored account",
        ),
        _contract(
            tool_reference("accounts.verify"),
            account_id,
            _object(account_id=IDENTIFIER, verified=BOOLEAN),
            authority="account.read",
            precondition="Account exists",
            postcondition="Verified flag matches the stored account",
        ),
        _contract(
            case_details,
            case_id,
            case_record,
            authority="case.read",
            precondition="Case exists",
            postcondition="Result matches the stored case details",
        ),
        _contract(
            tool_reference("cases.update"),
            _object(case_id=IDENTIFIER, expected_notes=TEXT, notes=TEXT),
            case_record,
            authority="case.write",
            side_effect="reversible",
            verify=case_details,
            precondition="Case is open, expected notes match, and replacement notes differ",
            postcondition="Case notes match the request and other fields are preserved",
        ),
        _contract(
            tool_reference("cases.close"),
            case_id,
            case_record,
            authority="case.write",
            side_effect="consequential",
            verify=case_details,
            precondition="Case is open",
            postcondition="Case status is closed",
        ),
        _contract(
            tool_reference("inventory.get_availability"),
            stock_key,
            _object(sku=IDENTIFIER, location=IDENTIFIER, available_count=STOCK_COUNT),
            authority="inventory.read",
            precondition="SKU exists at the specified location",
            postcondition="Available count matches stored inventory",
        ),
        _contract(
            reservation,
            stock_key,
            _object(sku=IDENTIFIER, location=IDENTIFIER, reserved_count=STOCK_COUNT),
            authority="inventory.read",
            precondition="SKU exists at the specified location",
            postcondition="Reserved count matches stored inventory",
        ),
        _contract(
            tool_reference("inventory.reserve"),
            stock_transfer,
            stock_record,
            authority="inventory.write",
            side_effect="reversible",
            verify=reservation,
            precondition="Available inventory covers the positive quantity",
            postcondition="Exactly the requested quantity moves from available to reserved",
        ),
        _contract(
            tool_reference("inventory.release"),
            stock_transfer,
            stock_record,
            authority="inventory.write",
            side_effect="reversible",
            verify=reservation,
            precondition="Reserved inventory covers the positive quantity",
            postcondition="Exactly the requested quantity moves from reserved to available",
        ),
        _contract(
            order,
            work_order_id,
            order_record,
            authority="work_order.read",
            precondition="Work order exists",
            postcondition="Result matches the stored work order",
        ),
        _contract(
            order_by_key,
            request_lookup,
            order_record,
            authority="work_order.read",
            precondition="A work order exists for the original key",
            postcondition="Result matches the work order stored under the key",
        ),
        _contract(
            tool_reference("work_orders.create"),
            _object(case_id=IDENTIFIER, action=REPLACEMENT),
            order_record,
            authority="case_replacement_approved",
            side_effect="consequential",
            verify=order_by_key,
            precondition="Case is open and approved with no non-cancelled replacement order",
            postcondition="One new replacement work order matches the case and request key",
        ),
        _contract(
            tool_reference("work_orders.cancel"),
            work_order_id,
            order_record,
            authority="work_order.write",
            side_effect="reversible",
            verify=order,
            precondition="Work order has created status",
            postcondition="Work order status is cancelled",
        ),
        _contract(
            tool_reference("notifications.send"),
            _object(case_id=IDENTIFIER, channel=CHANNEL),
            delivery_record,
            authority="notification.send",
            side_effect="consequential",
            verify=delivery,
            precondition="Case exists and channel is supported",
            postcondition="One delivered message matches the case, channel, and request key",
        ),
        _contract(
            delivery,
            request_lookup,
            delivery_record,
            authority="notification.read",
            precondition="A simulated message exists for the original key",
            postcondition="Result matches the message stored under the key",
        ),
    )
