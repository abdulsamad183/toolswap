"""Deterministic guard rules and recovery budgets."""

from collections import Counter
from dataclasses import dataclass, field

from toolswap.contracts import (
    SCHEMA_VERSION,
    Action,
    Budgets,
    GuardResult,
    JsonObject,
    RecoveryDecision,
    Reference,
    ToolContract,
)
from toolswap.determinism import canonical_json
from toolswap.recovery.gateway import Dispatch
from toolswap.recovery.records import Observation, Scope, Step, Verification
from toolswap.service_tools import ToolError, ToolRegistry

SEVERITY = {"none": 0, "reversible": 1, "consequential": 2}


def scope_violation(scope: Scope, contract: ToolContract, args: JsonObject) -> str | None:
    if not set(contract.authority).issubset(scope.permissions):
        return "permission_widening"
    if SEVERITY[contract.side_effect] > SEVERITY[scope.effect_ceiling]:
        return "effect_widening"
    quantity = args.get("quantity", 1)
    if not isinstance(quantity, (int, float)) or quantity > scope.max_targets:
        return "target_count_widening"
    for field_name, approved in scope.resources.items():
        if field_name in args and canonical_json(args[field_name]) != canonical_json(approved):
            return "resource_widening"
    return None


@dataclass
class BudgetLedger:
    limits: Budgets
    decisions: int = 0
    calls: Counter[tuple[Reference, bytes]] = field(default_factory=Counter)
    retries: Counter[Reference] = field(default_factory=Counter)

    def exhausted(self, tool: Reference, args: JsonObject, action: Action | None) -> str | None:
        if self.calls[(tool, canonical_json(args))] >= self.limits.equivalent_calls:
            return "equivalent_call_budget_exhausted"
        if action == Action.RETRY and self.retries[tool] >= self.limits.retries_per_tool:
            return "retry_budget_exhausted"
        return None

    def dispatch(self, tool: Reference, args: JsonObject, action: Action | None) -> None:
        self.calls[(tool, canonical_json(args))] += 1
        if action == Action.RETRY:
            self.retries[tool] += 1


@dataclass(frozen=True)
class Candidate:
    tool: Reference
    arguments: JsonObject
    request_key: str | None


@dataclass(frozen=True)
class GuardDecision:
    result: GuardResult
    candidate: Candidate | None = None


@dataclass(frozen=True)
class GuardContext:
    step: Step
    attempt: Dispatch
    verification: Verification
    needs_probe: bool
    trusted_reads: dict[Reference, Observation]
    scope: Scope
    allowed: dict[str, Reference]


def blocked(rule: str, reason: str, evidence: JsonObject) -> GuardDecision:
    return GuardDecision(
        GuardResult(
            schema_version=SCHEMA_VERSION,
            outcome="block",
            rule_id=rule,
            reason=reason,
            evidence=evidence,
        )
    )


def _allow(
    rule: str, reason: str, evidence: JsonObject, candidate: Candidate | None = None
) -> GuardDecision:
    return GuardDecision(
        GuardResult(
            schema_version=SCHEMA_VERSION,
            outcome="allow",
            rule_id=rule,
            reason=reason,
            evidence=evidence,
        ),
        candidate,
    )


def _repair_provenance(context: GuardContext, args: JsonObject) -> tuple[str | None, JsonObject]:
    sources: JsonObject = {}
    bindings = {binding.field: binding for binding in context.step.repair}
    for field_name in sorted(args.keys() | bindings.keys()):
        binding = bindings.get(field_name)
        if binding is None or field_name not in args:
            return field_name, sources
        if binding.source_tool is None:
            source = context.step.arguments
            reference: JsonObject = {"source": "task", "field": binding.source_field}
        else:
            read = context.trusted_reads.get(binding.source_tool)
            if read is None or read.result is None:
                return field_name, sources
            source = read.result
            for resource in context.scope.resources.keys() & context.step.arguments.keys():
                if resource not in source or canonical_json(source[resource]) != canonical_json(
                    context.step.arguments[resource]
                ):
                    return field_name, sources
            reference = {
                "source": "verified_observation",
                "call_id": read.call.call_id,
                "field": binding.source_field,
            }
        if binding.source_field not in source or (
            canonical_json(source[binding.source_field]) != canonical_json(args[field_name])
        ):
            return field_name, sources
        sources[field_name] = reference
    return None, sources


def authorize(
    decision: RecoveryDecision, context: GuardContext, registry: ToolRegistry, budget: BudgetLedger
) -> GuardDecision:
    action = decision.action
    original = context.attempt.visible.call
    verified = context.verification.status == "verified" and not context.needs_probe
    evidence: JsonObject = {
        "action": action.value,
        "call_id": original.call_id,
        "verification": context.verification.status,
    }
    if action == Action.STOP:
        return _allow("policy_stop", "Policy requested a stop", evidence)
    if action == Action.ACCEPT:
        if not verified:
            return blocked(
                "accept_requires_verification", "The current step is unresolved", evidence
            )
        return _allow("verified_accept", "The current step is verified", evidence)
    if action == Action.REQUEST_INFORMATION:
        if verified:
            return blocked(
                "information_already_verified", "The current step has verified evidence", evidence
            )
        evidence.update(
            tool=original.tool.model_dump(mode="json"),
            arguments=original.arguments,
            error=context.attempt.visible.error,
        )
        return _allow(
            "information_required", "The current step requires trusted information", evidence
        )

    target = context.allowed.get(decision.tool) if decision.tool is not None else None
    args = decision.arguments
    if target is None and action == Action.REQUIRE_CONFIRMATION and decision.tool is None:
        target, args = original.tool, original.arguments
    if target is None:
        return blocked("undeclared_tool", "The tool is outside the scenario allowlist", evidence)
    evidence.update(tool=target.model_dump(mode="json"), arguments=args)
    contract = registry.contract(target)
    key = original.request_key
    if action == Action.VERIFY:
        probe = context.step.probe
        if (
            probe is None
            or probe.tool != target
            or contract.side_effect != "none"
            or (canonical_json(probe.arguments) != canonical_json(args))
            or (key is not None and "idempotency_key" in args and args["idempotency_key"] != key)
        ):
            return blocked(
                "unsafe_verification_path",
                "Verification must use the pinned read and arguments",
                evidence,
            )
        key = None
        evidence["provenance"] = {"source": "pinned_verification_path", "call_id": original.call_id}
    elif action == Action.RETRY:
        if target != original.tool or canonical_json(args) != canonical_json(original.arguments):
            return blocked(
                "retry_changed_call", "A retry must reuse the exact call and trusted key", evidence
            )
        if contract.side_effect != "none" and context.verification.status != "not_applied":
            return blocked(
                "retry_requires_no_effect",
                "The write has no verified absence of an effect",
                evidence,
            )
        if not context.attempt.visible.retryable:
            return blocked("not_retryable", "The tool did not report a retryable failure", evidence)
        evidence["provenance"] = {"source": "original_call", "call_id": original.call_id}
    elif action == Action.REPAIR_ARGUMENTS:
        if (
            target != original.tool
            or not context.step.repair
            or context.attempt.actual is not None
            or ((context.attempt.visible.error or {}).get("code") != "invalid_arguments")
        ):
            return blocked(
                "repair_requires_schema_rejection",
                "Repair requires a pre-dispatch schema rejection",
                evidence,
            )
    elif action == Action.SWITCH_TOOL:
        alternative = next(
            (item for item in context.step.alternatives if item.tool == target), None
        )
        if (
            alternative is None
            or context.step.capability != alternative.capability
            or not alternative.fresh
            or SEVERITY[contract.side_effect]
            > SEVERITY[registry.contract(original.tool).side_effect]
        ):
            return blocked(
                "incompatible_fallback",
                "Fallback does not meet capability, freshness, or effect limits",
                evidence,
            )
        if canonical_json(alternative.arguments) != canonical_json(args):
            return blocked(
                "argument_without_provenance",
                "Fallback arguments differ from their trusted mapping",
                evidence,
            )
        if context.verification.status != "not_applied":
            return blocked(
                "fallback_requires_no_effect",
                "The original action may already have taken effect",
                evidence,
            )
        key = alternative.request_key
        evidence["provenance"] = {"source": "pinned_alternative", "call_id": original.call_id}
    elif target != original.tool or canonical_json(args) != canonical_json(original.arguments):
        return blocked(
            "confirmation_changed_call",
            "Confirmation must name the exact pending operation",
            evidence,
        )

    try:
        registry.validate_call(target, args, request_key=key)
    except ToolError as exc:
        return blocked("invalid_arguments", str(exc), evidence)
    if action == Action.REPAIR_ARGUMENTS:
        field_name, sources = _repair_provenance(context, args)
        evidence["provenance"] = sources
        if field_name is not None:
            evidence["field"] = field_name
            return blocked(
                "argument_without_provenance",
                "An argument lacks a matching trusted source",
                evidence,
            )
    violation = scope_violation(context.scope, contract, args)
    if violation is not None:
        evidence.update(
            trusted_scope=context.scope.model_dump(mode="json"),
            requested_permissions=list(contract.authority),
            proposed_effect=contract.side_effect,
        )
        return blocked(violation, "The proposed call exceeds the trusted scope", evidence)
    candidate = Candidate(target, args, key)
    evidence["request_key"] = key
    if (
        action == Action.REQUIRE_CONFIRMATION
        or contract.requires_confirmation
        or decision.requires_confirmation
    ):
        if target not in context.scope.confirmation_tools:
            return blocked(
                "confirmation_not_eligible",
                "The proposed action has no confirmation path",
                evidence,
            )
        evidence["proposed_effect"] = contract.side_effect
        return GuardDecision(
            GuardResult(
                schema_version=SCHEMA_VERSION,
                outcome="confirmation_required",
                rule_id="confirmation_required",
                reason="Explicit confirmation is required",
                evidence=evidence,
            ),
            candidate,
        )
    exhausted = budget.exhausted(target, args, action)
    if exhausted is not None:
        evidence.update(
            limits=budget.limits.model_dump(mode="json"),
            equivalent_calls=budget.calls[(target, canonical_json(args))],
            retries=budget.retries[target],
        )
        return blocked(exhausted, "Dispatch budget is exhausted", evidence)
    return _allow(
        "authorized_dispatch",
        "The call satisfies the pinned scope and recovery checks",
        evidence,
        candidate,
    )
