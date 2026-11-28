"""Versioned recovery data and trusted fixture definitions."""

from dataclasses import dataclass
from typing import Literal

from toolswap.contracts import (
    SCHEMA_VERSION,
    Budgets,
    ContentHash,
    Count,
    Identifier,
    JsonObject,
    Reference,
    VersionedRecord,
)

type Severity = Literal["none", "reversible", "consequential"]
type VerificationStatus = Literal["verified", "not_applied", "inconclusive"]
type FaultPoint = Literal["initial_before", "initial_after", "verification_before"]


class ToolCall(VersionedRecord):
    schema_version: Literal["1.0.0"] = SCHEMA_VERSION
    call_id: Identifier
    tool: Reference
    arguments: JsonObject
    request_key: str | None


class Observation(VersionedRecord):
    schema_version: Literal["1.0.0"] = SCHEMA_VERSION
    call: ToolCall
    result: JsonObject | None
    error: JsonObject | None
    retryable: bool


class PolicyInput(VersionedRecord):
    schema_version: Literal["1.0.0"] = SCHEMA_VERSION
    task: str
    allowed_tools: tuple[Reference, ...]
    budgets: Budgets
    remaining_recovery: Count
    observation: Observation
    history: tuple[Observation, ...]


@dataclass(frozen=True)
class Refusal:
    """Explicit adapter signal. Refusal text remains inert proposal data."""


class Verification(VersionedRecord):
    schema_version: Literal["1.0.0"] = SCHEMA_VERSION
    status: VerificationStatus
    tool: Reference
    postconditions: tuple[str, ...]
    call_id: Identifier
    probe_call_id: Identifier | None = None
    state_hash: ContentHash
    evidence: JsonObject


class Probe(VersionedRecord):
    schema_version: Literal["1.0.0"] = SCHEMA_VERSION
    tool: Reference
    arguments: JsonObject


class Alternative(Probe):
    capability: Identifier
    fresh: bool
    request_key: str | None = None


class Scope(VersionedRecord):
    schema_version: Literal["1.0.0"] = SCHEMA_VERSION
    permissions: tuple[Identifier, ...]
    resources: JsonObject
    max_targets: Count
    effect_ceiling: Severity
    confirmation_tools: tuple[Reference, ...] = ()


class RepairField(VersionedRecord):
    schema_version: Literal["1.0.0"] = SCHEMA_VERSION
    field: str
    source_field: str
    source_tool: Reference | None = None


class Step(VersionedRecord):
    schema_version: Literal["1.0.0"] = SCHEMA_VERSION
    tool: Reference
    arguments: JsonObject
    request_key: str | None = None
    expected: JsonObject
    table: str | None = None
    expected_row: JsonObject | None = None
    probe: Probe | None = None
    legacy_tool: Reference | None = None
    repair: tuple[RepairField, ...] = ()
    capability: Identifier | None = None
    alternatives: tuple[Alternative, ...] = ()


class Fault(VersionedRecord):
    schema_version: Literal["1.0.0"] = SCHEMA_VERSION
    step: Count
    point: FaultPoint

    @property
    def label(self) -> str:
        return f"step.{self.step}.{self.point}"
