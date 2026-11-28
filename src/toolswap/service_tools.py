"""Audit and invoke the versioned tools in the mock service environment."""

import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from jsonschema import Draft202012Validator
from pydantic import ValidationError

from toolswap.config import InputError
from toolswap.contracts import EffectRecord, JsonObject, Reference, Scenario, ToolContract
from toolswap.state import EffectOutcome, IdempotencyConflict, StateIntegrityError, StateStore

type ReadHandler = Callable[[sqlite3.Connection, JsonObject], JsonObject]
type WriteHandler = Callable[[sqlite3.Connection, JsonObject, str], JsonObject]


class ToolError(ValueError):
    """An expected tool error that can be represented in the contract's error schema."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ToolResult:
    observation: JsonObject | None = None
    error: JsonObject | None = None
    effect: EffectRecord | None = None


class ToolRegistry:
    """Internal dispatch for trusted callers with contract and handler parity checks."""

    def __init__(
        self,
        contracts: Iterable[ToolContract],
        reads: Mapping[Reference, ReadHandler],
        writes: Mapping[Reference, WriteHandler],
    ):
        self._contracts: dict[Reference, ToolContract] = {}
        self._reads = dict(reads)
        self._writes = dict(writes)
        for contract in contracts:
            ref = Reference(id=contract.tool_id, version=contract.version)
            if ref in self._contracts:
                raise InputError(f"duplicate contract for {ref.id}@{ref.version}")
            try:
                # Validate and copy schemas even when a caller supplies an existing record.
                self._contracts[ref] = ToolContract.model_validate(contract.model_dump())
            except ValidationError as exc:
                raise InputError(f"invalid contract for {ref.id}@{ref.version}") from exc
        self._audit()

    def _audit(self) -> None:
        contracts, reads, writes = self._contracts.keys(), self._reads.keys(), self._writes.keys()
        differences = (contracts ^ (reads | writes)) | (reads & writes)
        if differences:
            mismatched = ", ".join(sorted(f"{ref.id}@{ref.version}" for ref in differences))
            raise InputError(f"contracts and handlers must match exactly: {mismatched}")

        for ref, contract in self._contracts.items():
            label = f"{ref.id}@{ref.version}"
            if not all((contract.authority, contract.preconditions, contract.postconditions)):
                raise InputError(f"incomplete safety metadata for {label}")
            if ref in reads:
                if contract.side_effect != "none" or contract.idempotency != "natural":
                    raise InputError(f"read handler requires a read-only contract for {label}")
            elif contract.side_effect == "none" or contract.idempotency != "key_required":
                raise InputError(f"write handler requires a keyed write contract for {label}")
            elif contract.verification_tool is None:
                raise InputError(f"write contract requires a verification path for {label}")
            if contract.verification_tool is not None:
                target = self._contracts.get(contract.verification_tool)
                if target is None or target.side_effect != "none" or target.requires_confirmation:
                    raise InputError(f"verification path must be a registered read for {label}")
            if contract.compensation_tool is not None and contract.compensation_tool not in writes:
                raise InputError(f"compensation path must be a registered write for {label}")

    def _resolve(self, ref: Reference) -> ToolContract:
        try:
            return self._contracts[ref]
        except KeyError as exc:
            raise InputError(f"unsupported service tool {ref.id}@{ref.version}") from exc

    def contract(self, ref: Reference) -> ToolContract:
        return self._resolve(ref).model_copy(deep=True)

    def audit_scenario(self, scenario: Scenario) -> None:
        for ref in scenario.allowed_tools:
            self._resolve(ref)

    def validate_call(
        self, ref: Reference, arguments: JsonObject, *, request_key: str | None = None
    ) -> None:
        """Check the exact contract without opening the environment."""
        _validate_call(self._resolve(ref), arguments, request_key)

    def invoke(
        self,
        store: StateStore,
        ref: Reference,
        arguments: JsonObject,
        *,
        request_key: str | None = None,
    ) -> ToolResult:
        contract = self._resolve(ref)
        try:
            _validate_call(contract, arguments, request_key)
            reader = self._reads.get(ref)
            if reader is not None:
                with store.read() as db:
                    observation = reader(db, arguments)
                _validate_observation(contract, observation)
                return ToolResult(observation=observation)

            assert request_key is not None  # Checked against the write contract above.
            key = request_key
            writer = self._writes[ref]

            def mutate(db: sqlite3.Connection) -> EffectOutcome:
                observation = writer(db, arguments, key)
                _validate_observation(contract, observation)
                return EffectOutcome("complete", observation)

            effect = store.apply_effect(ref, key, arguments, mutate)
            if effect.status == "partial":
                return _failure(
                    contract,
                    ToolError("partial_effect", "A prior partial effect requires verification"),
                    effect=effect,
                )
            _validate_observation(contract, effect.result)
            return ToolResult(observation=effect.result, effect=effect)
        except ToolError as exc:
            return _failure(contract, exc)
        except IdempotencyConflict as exc:
            return _failure(contract, ToolError("idempotency_conflict", str(exc)))


def _validate_call(contract: ToolContract, arguments: JsonObject, request_key: str | None) -> None:
    if not Draft202012Validator(contract.input_schema).is_valid(arguments):
        raise ToolError("invalid_arguments", "Arguments violate the tool schema")
    if contract.side_effect != "none" and (
        not isinstance(request_key, str) or not request_key.strip()
    ):
        raise ToolError("invalid_arguments", "A write requires a nonempty request key")


def _validate_observation(contract: ToolContract, observation: JsonObject) -> None:
    if not Draft202012Validator(contract.output_schema).is_valid(observation):
        raise StateIntegrityError(f"result violates the contract for {contract.tool_id}")


def _failure(
    contract: ToolContract, error: ToolError, *, effect: EffectRecord | None = None
) -> ToolResult:
    data: JsonObject = {"code": error.code, "message": str(error)}
    if not Draft202012Validator(contract.error_schema).is_valid(data):
        raise StateIntegrityError(f"error violates the contract for {contract.tool_id}")
    return ToolResult(error=data, effect=effect)
