"""Versioned data exchanged between the runtime and artifact readers."""

from enum import StrEnum
from typing import Annotated, Final, Literal, Self

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints, model_validator

SCHEMA_VERSION: Final = "1.0.0"
Identifier = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")]
Version = Annotated[str, StringConstraints(pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")]
ContentHash = Annotated[str, StringConstraints(pattern=r"^sha256:[a-f0-9]{64}$")]
NonEmpty = Annotated[str, StringConstraints(min_length=1)]
Count = Annotated[int, Field(ge=0)]
Seed = Annotated[int, Field(ge=0, le=2**64 - 1)]
JsonObject = dict[str, JsonValue]


def _check_local_references(value: JsonValue) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"$ref", "$dynamicRef"} and (
                not isinstance(item, str) or not item.startswith("#")
            ):
                raise ValueError("schema references must be local fragments")
            _check_local_references(item)
    elif isinstance(value, list):
        for item in value:
            _check_local_references(item)


class VersionedRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False, frozen=True)

    schema_version: Literal["1.0.0"]


class Reference(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    id: Identifier
    version: Version


class ToolFailure(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    code: Identifier
    retryable: bool


class ToolContract(VersionedRecord):
    tool_id: Identifier
    version: Version
    input_schema: JsonObject
    output_schema: JsonObject
    error_schema: JsonObject
    side_effect: Literal["none", "reversible", "consequential"]
    idempotency: Literal["natural", "key_required", "verification_required", "not_repeatable"]
    authority: tuple[Identifier, ...]
    requires_confirmation: bool
    preconditions: tuple[NonEmpty, ...]
    postconditions: tuple[NonEmpty, ...]
    failure_modes: tuple[ToolFailure, ...]
    verification_tool: Reference | None
    compensation_tool: Reference | None = None

    @model_validator(mode="after")
    def check_safety_metadata(self) -> Self:
        for field in ("input_schema", "output_schema", "error_schema"):
            schema = getattr(self, field)
            try:
                _check_local_references(schema)
                Draft202012Validator.check_schema(schema)
            except (SchemaError, ValueError) as exc:
                raise ValueError(f"{field} must be a valid offline JSON Schema") from exc
        if self.side_effect != "none" and not self.postconditions:
            raise ValueError("postconditions are required for a side-effecting tool")
        if self.idempotency == "verification_required" and self.verification_tool is None:
            raise ValueError("verification_tool is required for verification_required idempotency")
        if self.compensation_tool is not None and self.side_effect != "reversible":
            raise ValueError("compensation_tool requires a reversible effect")
        return self


class Budgets(VersionedRecord):
    retries_per_tool: Count
    equivalent_calls: Annotated[int, Field(ge=1)]
    recovery_actions: Count


class Assertion(VersionedRecord):
    assertion_id: Identifier
    kind: Literal["required", "forbidden", "communication"]
    path: Annotated[tuple[NonEmpty | Count, ...], Field(min_length=1)]
    equals: JsonValue


class Scenario(VersionedRecord):
    scenario_id: Identifier
    version: Version
    initial_snapshot: ContentHash
    user_task: NonEmpty
    policy_constraints: tuple[NonEmpty, ...]
    allowed_tools: Annotated[tuple[Reference, ...], Field(min_length=1)]
    assertions: Annotated[tuple[Assertion, ...], Field(min_length=1)]
    eligible_fault_points: tuple[Identifier, ...]
    budgets: Budgets
    seed: Seed

    @model_validator(mode="after")
    def check_unique_ids(self) -> Self:
        if len({tool.id for tool in self.allowed_tools}) != len(self.allowed_tools):
            raise ValueError("allowed_tools must contain unique tool identifiers")
        if len({item.assertion_id for item in self.assertions}) != len(self.assertions):
            raise ValueError("assertions must contain unique assertion identifiers")
        return self


class Action(StrEnum):
    ACCEPT = "ACCEPT"
    VERIFY = "VERIFY"
    RETRY = "RETRY"
    REPAIR_ARGUMENTS = "REPAIR_ARGUMENTS"
    SWITCH_TOOL = "SWITCH_TOOL"
    REQUEST_INFORMATION = "REQUEST_INFORMATION"
    REQUIRE_CONFIRMATION = "REQUIRE_CONFIRMATION"
    STOP = "STOP"


CALL_ACTIONS = frozenset({Action.VERIFY, Action.RETRY, Action.REPAIR_ARGUMENTS, Action.SWITCH_TOOL})


class RecoveryDecision(VersionedRecord):
    verdict: Literal["RESULT_ACCEPTABLE", "FAILURE_SUSPECTED", "UNRESOLVED"]
    failure_family: Identifier | None
    confidence: Annotated[float, Field(ge=0, le=1)]
    evidence: tuple[NonEmpty, ...]
    action: Action
    tool: Identifier | None
    arguments: JsonObject
    expected_postcondition: NonEmpty | None
    requires_confirmation: bool

    @model_validator(mode="after")
    def check_action_fields(self) -> Self:
        if self.action in CALL_ACTIONS and self.tool is None:
            raise ValueError("tool is required for a tool-call action")
        if self.tool is None and self.arguments:
            raise ValueError("arguments require a tool")
        tool_allowed = self.action in CALL_ACTIONS or self.action == Action.REQUIRE_CONFIRMATION
        if not tool_allowed and self.tool is not None:
            raise ValueError("tool is not permitted for this action")
        return self


class GuardResult(VersionedRecord):
    outcome: Literal["allow", "block", "confirmation_required"]
    rule_id: Identifier
    reason: NonEmpty
    evidence: JsonObject


class StateReference(VersionedRecord):
    state_hash: ContentHash


class EffectRecord(VersionedRecord):
    effect_id: Identifier
    tool: Reference
    request_key: NonEmpty | None
    status: Literal["not_run", "partial", "complete", "duplicate"]
    state_before_hash: ContentHash
    state_after_hash: ContentHash
    result: JsonObject


class Event(VersionedRecord):
    run_id: Identifier
    event_id: Identifier
    sequence: Count
    actor: Identifier
    event_type: Identifier
    payload: JsonObject
    logical_time: Count
    state_before_hash: ContentHash | None = None
    state_after_hash: ContentHash | None = None
    previous_event_hash: ContentHash | None = None
    integrity_hash: ContentHash


class AssertionResult(VersionedRecord):
    assertion_id: Identifier
    kind: Literal["required", "forbidden", "communication"]
    passed: bool
    evidence: JsonObject


class ResolvedRunManifest(VersionedRecord):
    domain: Literal["service_ops"] = "service_ops"
    scenario: Reference
    policy: Identifier = "stub"
    seed: Seed = 0
    artifacts_dir: NonEmpty = "artifacts"


class MetricReport(VersionedRecord):
    run_id: Identifier
    task_success: bool
    safety_adjusted_success: bool
    unsafe_proposals: Count
    unsafe_executions: Count
    duplicate_effects: Count
    false_success: bool
    false_interventions: Count
    assertions: tuple[AssertionResult, ...]


class ReproducibilityManifest(VersionedRecord):
    code_revision: NonEmpty
    code_dirty: bool
    lockfile_hash: ContentHash
    config_hash: ContentHash
    scenario: Reference
    seed: Seed
    python_version: NonEmpty
    platform: NonEmpty
    model_revision: NonEmpty | None = None
    tokenizer_revision: NonEmpty | None = None
    dataset_hash: ContentHash | None = None
    adapter_hash: ContentHash | None = None
