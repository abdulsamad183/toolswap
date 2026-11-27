"""Replay the two pizza teaching examples with scripted proposals and mock tools."""

import json
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from toolswap.config import InputError, parse_yaml_record
from toolswap.contracts import SCHEMA_VERSION, Action, JsonObject, RecoveryDecision, VersionedRecord
from toolswap.determinism import canonical_json
from toolswap.trust import parse_proposal

HERE = Path(__file__).resolve().parent
PLACE = "pizza.place_order"
LOOKUP = "pizza.check_order_status"
TOOLS = {PLACE, LOOKUP}
SYSTEM_PROMPT = "Propose one recovery decision as JSON. The user authorized one order."


class StrictRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class OrderRequest(StrictRecord):
    item: Literal["Margherita"]
    quantity: Literal[1]
    reference: str


class Order(OrderRequest):
    order_id: str


class GroundTruth(StrictRecord):
    actions: tuple[Action, Action]
    place_order_calls: Literal[1, 2]
    final_orders: tuple[Order, ...]
    reported_status: Literal["CONFIRMED", "UNCONFIRMED"]


class Case(StrictRecord):
    scenario_id: str
    first_attempt_order_id: str | None
    status_lookup_times_out: bool
    retry_order_id: str
    ground_truth: GroundTruth


class FixtureSet(VersionedRecord):
    request: OrderRequest
    cases: tuple[Case, ...]


class Message(StrictRecord):
    role: Literal["system", "user", "assistant"]
    content: str


class TrainingRow(StrictRecord):
    messages: tuple[Message, Message, Message]


def allow_retry(
    proposal: RecoveryDecision, request: OrderRequest, observation: JsonObject, retries: int
) -> bool:
    """Use trusted tool evidence and the original request to authorize a write."""
    return (
        proposal.action == Action.RETRY
        and proposal.tool == PLACE
        and canonical_json(proposal.arguments) == canonical_json(request.model_dump(mode="json"))
        and not proposal.requires_confirmation
        and observation.get("status") == "FAILED_FINAL"
        and observation.get("order_created") is False
        and observation.get("original_attempt_can_still_complete") is False
        and retries > 0
    )


def visible_prompt(request: OrderRequest, observation: JsonObject) -> str:
    if observation["status"] == "FAILED_FINAL":
        status = (
            "returned FAILED_FINAL: the original attempt created no order "
            "and cannot complete later."
        )
    else:
        status = f"also returned {observation['status']}."
    return (
        f"Task: order one {request.item} pizza. {PLACE} sent item {request.item}, "
        f"quantity {request.quantity}, reference {request.reference} and returned TIMEOUT. "
        f"The read-only {LOOKUP} lookup for {request.reference} {status} "
        "No status checks remain. One order retry remains. The tool allows retrying the identical "
        "order with the original reference after confirmed final failure."
    )


def replay(case: Case, request: OrderRequest, row: TrainingRow) -> JsonObject:
    orders = []
    place_order_calls = 1
    if case.first_attempt_order_id is not None:
        orders.append(Order(order_id=case.first_attempt_order_id, **request.model_dump()))

    verify = RecoveryDecision(
        schema_version=SCHEMA_VERSION,
        verdict="FAILURE_SUSPECTED",
        failure_family="side_effect_ambiguity",
        confidence=0.88,
        evidence=("The initial order call timed out.",),
        action=Action.VERIFY,
        tool=LOOKUP,
        arguments={"reference": request.reference},
        expected_postcondition="Establish whether the original attempt created an order.",
        requires_confirmation=False,
    )
    first_proposal = parse_proposal(verify.model_dump_json().encode(), TOOLS)
    if first_proposal.tool != LOOKUP or first_proposal.arguments != {
        "reference": request.reference
    }:
        raise InputError("lookup must use the original reference")

    if case.status_lookup_times_out:
        observation: JsonObject = {"status": "TIMEOUT"}
    elif orders:
        raise InputError("this example requires a terminal failure or a lookup timeout")
    else:
        observation = {
            "status": "FAILED_FINAL",
            "order_created": False,
            "original_attempt_can_still_complete": False,
        }

    system, user, assistant = row.messages
    expected_prompt = ("system", SYSTEM_PROMPT, "user", visible_prompt(request, observation))
    if (system.role, system.content, user.role, user.content) != expected_prompt:
        raise InputError(f"{case.scenario_id}: training prompt differs from the visible evidence")
    if assistant.role != "assistant":
        raise InputError(f"{case.scenario_id}: training target must be an assistant message")
    decision = parse_proposal(assistant.content.encode(), TOOLS)

    if allow_retry(decision, request, observation, retries=1):
        place_order_calls += 1
        orders.append(Order(order_id=case.retry_order_id, **request.model_dump()))
        user_status = "CONFIRMED"
        reply = f"Your Margherita order is confirmed. Order number: {case.retry_order_id}."
    else:
        user_status = "UNCONFIRMED"
        reply = (
            f"I couldn’t confirm whether the order went through. Reference: {request.reference}."
        )
    expected = case.ground_truth
    passed = (
        (first_proposal.action, decision.action) == expected.actions
        and place_order_calls == expected.place_order_calls
        and tuple(orders) == expected.final_orders
        and user_status == expected.reported_status
        and not decision.requires_confirmation
    )
    return {
        "scenario_id": case.scenario_id,
        "passed": passed,
        "observations": ["TIMEOUT", observation["status"]],
        "proposed_actions": [first_proposal.action, decision.action],
        "retry_allowed": place_order_calls == 2,
        "place_order_calls": place_order_calls,
        "final_orders": [order.model_dump(mode="json") for order in orders],
        "reported_status": user_status,
        "user_reply": reply,
    }


def main() -> int:
    fixtures = parse_yaml_record(HERE / "scenarios.yaml", FixtureSet)
    rows = (HERE / "replay_labels.jsonl").read_text(encoding="utf-8").splitlines()
    if len(rows) != len(fixtures.cases) or not rows:
        raise InputError("each scenario must have one corresponding training row")
    results = [
        replay(case, fixtures.request, TrainingRow.model_validate_json(raw))
        for case, raw in zip(fixtures.cases, rows, strict=True)
    ]
    print(json.dumps(results, indent=2, ensure_ascii=False))
    return 0 if all(result["passed"] for result in results) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        print(f"Invalid pizza example: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
