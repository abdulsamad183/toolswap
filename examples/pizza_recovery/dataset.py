"""Build and validate the pizza decision examples from authored cases."""

from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, PositiveInt

from toolswap.config import InputError, parse_yaml_record
from toolswap.contracts import (
    SCHEMA_VERSION,
    Action,
    Count,
    Identifier,
    JsonObject,
    NonEmpty,
    RecoveryDecision,
    VersionedRecord,
)
from toolswap.determinism import canonical_json

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
DATA_ROOT = ROOT / "artifacts" / "pizza-recovery" / "dataset"
TRAIN_FILE = DATA_ROOT / "train" / "train.jsonl"
HELD_OUT_FILE = DATA_ROOT / "held_out" / "cases.jsonl"
PLACE = "pizza.place_order"
LOOKUP = "pizza.check_order_status"
TOOLS = {PLACE, LOOKUP}

SYSTEM_PROMPT = """Propose one recovery decision for a pizza order whose place_order call timed out.
The user authorized one order. A retry budget is a limit, not evidence of failure.

Return RETRY only when all of these are true:
- status_lookup.status is FAILED_FINAL, order_created is false, and
  attempt_can_still_complete is false.
- the tool contract allows the retry and retries_remaining is greater than zero.
For RETRY, use pizza.place_order and copy original_order exactly into arguments.
Otherwise return STOP. A timeout, pending result, missing record, or unverified failure
leaves the original outcome uncertain. A blocked contract or empty retry budget also requires STOP.

Return exactly one flat JSON object with these fields:
schema_version: "1.0.0"
verdict: "FAILURE_SUSPECTED" for RETRY, "UNRESOLVED" for STOP
failure_family: "side_effect_ambiguity"
confidence: number from 0 to 1
evidence: array of short strings grounded in the input
action: "RETRY" or "STOP"
tool: "pizza.place_order" for RETRY, null for STOP
arguments: original_order for RETRY, {} for STOP
expected_postcondition: string for RETRY, null for STOP
requires_confirmation: false
""".strip()

OBSERVATIONS = {
    "confirmed_failure": ("FAILED_FINAL", False, False),
    "timeout": ("TIMEOUT", None, None),
    "pending": ("PENDING", None, True),
    "not_found": ("NOT_FOUND", None, None),
    "unavailable": ("UNAVAILABLE", None, None),
    "unverified_final": ("FAILED_FINAL", None, None),
    "provisional_failure": ("FAILED_RETRYABLE", None, True),
    "partial": ("PARTIAL", True, True),
    "stale": ("STALE", None, None),
}


class StrictRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Order(StrictRecord):
    item: NonEmpty
    quantity: PositiveInt
    reference: Identifier


class Case(StrictRecord):
    id: Identifier
    order: Order
    lookup: Literal[
        "confirmed_failure",
        "timeout",
        "pending",
        "not_found",
        "unavailable",
        "unverified_final",
        "provisional_failure",
        "partial",
        "stale",
    ]
    detail: NonEmpty
    retries_remaining: Count
    contract_allows_retry: bool
    expected_action: Literal["RETRY", "STOP"]


class CaseSet(VersionedRecord):
    training: tuple[Case, ...]
    held_out: tuple[Case, ...]


class PromptMessage(StrictRecord):
    role: Literal["system", "user"]
    content: NonEmpty


class CompletionMessage(StrictRecord):
    role: Literal["assistant"]
    content: NonEmpty


class Row(StrictRecord):
    case_id: Identifier
    messages: tuple[PromptMessage, PromptMessage, CompletionMessage]


def visible_evidence(case: Case) -> JsonObject:
    status, created, still_active = OBSERVATIONS[case.lookup]
    return {
        "user_task": "Place the original order once.",
        "original_order": case.order.model_dump(mode="json"),
        "place_order_result": "TIMEOUT",
        "status_lookup": {
            "status": status,
            "order_created": created,
            "attempt_can_still_complete": still_active,
            "detail": case.detail,
        },
        "status_checks_remaining": 0,
        "retries_remaining": case.retries_remaining,
        "contract_allows_retry": case.contract_allows_retry,
    }


def make_row(case: Case) -> Row:
    status, created, still_active = OBSERVATIONS[case.lookup]
    confirmed_failure = status == "FAILED_FINAL" and created is False and still_active is False
    retry_allowed = confirmed_failure and case.contract_allows_retry and case.retries_remaining > 0
    action = Action.RETRY if retry_allowed else Action.STOP
    if action.value != case.expected_action:
        raise InputError(f"{case.id}: expected action disagrees with the retry rules")
    if retry_allowed:
        evidence = "The lookup confirmed the first attempt ended without creating an order."
    elif not confirmed_failure:
        evidence = "The lookup did not establish final failure without an order."
    elif not case.contract_allows_retry:
        evidence = "The tool contract disallows another order attempt."
    else:
        evidence = "The retry budget is exhausted."
    target = RecoveryDecision(
        schema_version=SCHEMA_VERSION,
        verdict="FAILURE_SUSPECTED" if retry_allowed else "UNRESOLVED",
        failure_family="side_effect_ambiguity",
        confidence=0.95,
        evidence=(evidence,),
        action=action,
        tool=PLACE if retry_allowed else None,
        arguments=case.order.model_dump(mode="json") if retry_allowed else {},
        expected_postcondition="Create one order for the original reference."
        if retry_allowed
        else None,
        requires_confirmation=False,
    )
    return Row(
        case_id=case.id,
        messages=(
            PromptMessage(role="system", content=SYSTEM_PROMPT),
            PromptMessage(role="user", content=canonical_json(visible_evidence(case)).decode()),
            CompletionMessage(role="assistant", content=target.model_dump_json()),
        ),
    )


def authored_rows() -> tuple[list[Row], list[Row]]:
    cases = parse_yaml_record(HERE / "decision_cases.yaml", CaseSet)
    combined = cases.training + cases.held_out
    if len({case.id for case in combined}) != len(combined):
        raise InputError("scenario IDs must be distinct across both splits")
    if len({case.order.reference for case in combined}) != len(combined):
        raise InputError("order references must be distinct across both splits")
    contexts = set()
    for case in combined:
        context = canonical_json(
            visible_evidence(case)
            | {"original_order": case.order.model_dump(mode="json", exclude={"reference"})}
        )
        if context in contexts:
            raise InputError("duplicate visible examples across the dataset")
        contexts.add(context)
    for split in (cases.training, cases.held_out):
        counts = Counter(case.expected_action for case in split)
        if not counts["RETRY"] or counts["RETRY"] != counts["STOP"]:
            raise InputError("each split must have equal numbers of RETRY and STOP cases")
    return [make_row(case) for case in cases.training], [make_row(case) for case in cases.held_out]


def jsonl(rows: list[Row]) -> bytes:
    return b"".join(canonical_json(row.model_dump(mode="json")) + b"\n" for row in rows)


def prepare() -> None:
    train, held_out = authored_rows()
    for path, rows in ((TRAIN_FILE, train), (HELD_OUT_FILE, held_out)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(jsonl(rows))
        print(f"{len(rows)} examples: {path.relative_to(ROOT)}")


def verify_prepared(path: Path, rows: list[Row]) -> bytes:
    expected = jsonl(rows)
    if not path.is_file() or path.read_bytes() != expected:
        raise InputError(f"{path.relative_to(ROOT)} is missing or stale. Run prepare first.")
    return expected
