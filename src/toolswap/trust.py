"""Decode untrusted policy proposals as bounded data."""

import json
import math
from collections.abc import Collection

from pydantic import JsonValue, ValidationError

from toolswap.contracts import RecoveryDecision

MAX_PROPOSAL_BYTES = 16 * 1024


class ProposalError(ValueError):
    """A stable rejection reason for an untrusted proposal."""


def _unique_fields(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    fields: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in fields:
            raise ProposalError("malformed_json")
        fields[key] = value
    return fields


def _nonfinite_value(_: str) -> None:
    raise ProposalError("malformed_json")


def _finite_float(raw: str) -> float:
    value = float(raw)
    if not math.isfinite(value):
        raise ProposalError("malformed_json")
    return value


def parse_proposal(raw: bytes, allowed_tools: Collection[str]) -> RecoveryDecision:
    if len(raw) > MAX_PROPOSAL_BYTES:
        raise ProposalError("overlong_output")
    try:
        json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_fields,
            parse_constant=_nonfinite_value,
            parse_float=_finite_float,
        )
    except (ValueError, RecursionError) as exc:
        raise ProposalError("malformed_json") from exc
    try:
        proposal = RecoveryDecision.model_validate_json(raw)
    except ValidationError as exc:
        raise ProposalError("invalid_decision") from exc
    if proposal.tool is not None and proposal.tool not in allowed_tools:
        raise ProposalError("undeclared_tool")
    return proposal
