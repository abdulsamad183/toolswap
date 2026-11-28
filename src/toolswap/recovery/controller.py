"""Bounded recovery transitions for one trusted sequence of tool operations."""

from dataclasses import dataclass, field
from typing import Protocol

from toolswap.contracts import (
    SCHEMA_VERSION,
    Action,
    EffectRecord,
    Event,
    JsonObject,
    Reference,
    Scenario,
)
from toolswap.determinism import canonical_json, content_hash
from toolswap.recovery.gateway import Dispatch, Gateway, Timeout
from toolswap.recovery.guard import BudgetLedger, GuardContext, authorize, blocked, scope_violation
from toolswap.recovery.records import (
    Fault,
    Observation,
    PolicyInput,
    Refusal,
    Scope,
    Step,
    ToolCall,
    Verification,
)
from toolswap.recovery.verifier import verify
from toolswap.replay import TraceRecord, TraceRecorder
from toolswap.state import StateIntegrityError
from toolswap.trust import MAX_PROPOSAL_BYTES, ProposalError, parse_proposal

HISTORY_LIMIT = 8


class RecoveryPolicy(Protocol):
    def __call__(self, request: PolicyInput) -> bytes | Refusal: ...


@dataclass
class Controller:
    gateway: Gateway
    scenario: Scenario
    scope: Scope
    policy: RecoveryPolicy
    faults: tuple[Fault, ...] = ()
    trace: TraceRecorder = field(init=False)
    snapshots: dict[str, bytes] = field(default_factory=dict, init=False)
    effects: dict[str, EffectRecord] = field(default_factory=dict, init=False)
    active_step: int | None = field(default=None, init=False)
    active_tool: Reference | None = field(default=None, init=False)
    state_hash: str = field(init=False)
    _interrupted: ToolCall | None = field(default=None, init=False)
    _verification_id: str = field(init=False)
    timings_ns: dict[str, int] = field(default_factory=dict, init=False)
    trusted_reads: dict[Reference, Observation] = field(default_factory=dict, init=False)
    history: list[Observation] = field(default_factory=list, init=False)
    budget: BudgetLedger = field(init=False)
    allowed: dict[str, Reference] = field(init=False)

    def __post_init__(self) -> None:
        self.budget = BudgetLedger(self.scenario.budgets)
        self.allowed = {ref.id: ref for ref in self.scenario.allowed_tools}
        self.trace = TraceRecorder(self.gateway.run_id, self.gateway.seed)
        self.state_hash = self.scenario.initial_snapshot

    @property
    def records(self) -> tuple[Event, ...]:
        return self.trace.events

    def emit(self, actor: str, event_type: str, payload: JsonObject) -> Event:
        return self.trace.append(
            TraceRecord(actor, event_type, payload, self.state_hash, self.state_hash)
        )

    def _remember_snapshot(self, state_hash: str) -> None:
        if state_hash not in self.snapshots:
            self.snapshots[state_hash] = self.gateway.store.resolve_snapshot(state_hash)

    def _record_result(
        self, event_type: str, payload: JsonObject, after: str, effect: EffectRecord | None
    ) -> None:
        self._remember_snapshot(after)
        self.trace.append(TraceRecord("gateway", event_type, payload, self.state_hash, after))
        self.state_hash = after
        self._interrupted = None
        if effect is not None and effect.effect_id not in self.effects:
            self.effects[effect.effect_id] = effect
            self.emit("environment", "effect_recorded", effect.model_dump(mode="json"))

    def recover_interrupted_effect(self) -> None:
        """Preserve a ledger entry if an infrastructure error hid the registry response."""
        call = self._interrupted
        if call is None or call.request_key is None:
            return
        new = [e for e in self.gateway.store.effects() if e.effect_id not in self.effects]
        if len(new) == 1:
            effect = new[0]
            if (effect.tool, effect.request_key, effect.state_before_hash) == (
                call.tool,
                call.request_key,
                self.state_hash,
            ):
                self._record_result(
                    "tool_interrupted",
                    {"call": call.model_dump(mode="json"), "effect_id": effect.effect_id},
                    effect.state_after_hash,
                    effect,
                )

    def _record_verification(self, checked: Verification) -> None:
        self._verification_id = self.emit(
            "verifier", "verification", checked.model_dump(mode="json") | {"step": self.active_step}
        ).event_id

    def _dispatch(
        self,
        tool: Reference,
        arguments: JsonObject,
        key: str | None,
        action: Action | None,
        authorization_id: str,
        timeout: Timeout | None = None,
    ) -> Dispatch:
        def record_call(event_type: str, call: ToolCall, before: str) -> None:
            if before != self.state_hash:
                raise StateIntegrityError("tool call does not start at the recorded state")
            self.emit(
                "gateway",
                event_type,
                {"call": call.model_dump(mode="json"), "authorization_id": authorization_id},
            )
            if event_type == "tool_dispatch":
                self._interrupted = call

        self.budget.dispatch(tool, arguments, action)
        outcome = self.gateway.execute(
            tool, arguments, request_key=key, timeout=timeout, record_call=record_call
        )
        self.timings_ns[outcome.visible.call.call_id] = outcome.elapsed_ns
        self.history.append(outcome.visible)
        del self.history[:-HISTORY_LIMIT]
        effect = outcome.actual.effect if outcome.actual is not None else None
        self._record_result(
            "tool_result",
            {
                "visible": outcome.visible.model_dump(mode="json"),
                "entered_environment": outcome.actual is not None,
                "effect_id": effect.effect_id if effect is not None else None,
                "fault": outcome.timeout,
            },
            outcome.after,
            effect,
        )
        return outcome

    def _remember_read(self, observation: Observation) -> None:
        contract = self.gateway.registry.contract(observation.call.tool)
        if contract.side_effect == "none" and observation.result is not None:
            self.trusted_reads[observation.call.tool] = observation

    def _policy_input(self, observation: Observation) -> PolicyInput:
        request = PolicyInput(
            task=self.scenario.user_task,
            allowed_tools=self.scenario.allowed_tools,
            budgets=self.scenario.budgets,
            remaining_recovery=self.budget.limits.recovery_actions - self.budget.decisions,
            observation=observation,
            history=tuple(self.history[:-1]),
        )
        while len(canonical_json(request.model_dump(mode="json"))) > MAX_PROPOSAL_BYTES:
            if not request.history:
                raise ProposalError("policy_input_overlong")
            request = request.model_copy(update={"history": request.history[1:]})
        return request

    def run(
        self, steps: tuple[Step, ...], claims: JsonObject, *, manifest_hash: str | None = None
    ) -> JsonObject:
        self._remember_snapshot(self.state_hash)
        self.emit("environment", "snapshot_loaded", {"scenario": self.scenario.scenario_id})
        self.emit(
            "controller",
            "run_started",
            {
                "budgets": self.scenario.budgets.model_dump(mode="json"),
                "scope": self.scope.model_dump(mode="json"),
                "eligible_fault_points": list(self.scenario.eligible_fault_points),
                "step_count": len(steps),
                "manifest_hash": manifest_hash,
            },
        )
        self.emit("user", "user_task", {"task": self.scenario.user_task})
        for index, step in enumerate(steps):
            self.active_step, self.active_tool = index, step.tool
            incomplete = self._run_step(index, step)
            if incomplete is not None:
                return incomplete
            self.emit(
                "controller",
                "step_completed",
                {"step": index, "verification_id": self._verification_id},
            )
        self.active_step, self.active_tool = None, None
        return claims | {"schema_version": SCHEMA_VERSION, "status": "complete"}

    def _run_step(self, index: int, step: Step) -> JsonObject | None:
        def incomplete(rule: str, evidence: JsonObject | None = None) -> JsonObject:
            response: JsonObject = {
                "schema_version": SCHEMA_VERSION,
                "status": "incomplete",
                "reason": rule,
                "unresolved_step": index,
                "tool": step.tool.model_dump(mode="json"),
            }
            if evidence is not None:
                response["handoff"] = evidence
            return response

        exhausted = self.budget.exhausted(step.tool, step.arguments, None)
        if exhausted is not None:
            details = blocked(exhausted, "Initial dispatch budget is exhausted", {"step": index})
            self.emit("guard", "guard_result", details.result.model_dump(mode="json"))
            return incomplete(exhausted)
        contract = self.gateway.registry.contract(step.tool)
        violation = scope_violation(self.scope, contract, step.arguments)
        if contract.requires_confirmation:
            violation = "confirmation_not_eligible"
        if self.allowed.get(step.tool.id) != step.tool:
            violation = "undeclared_tool"
        if violation is not None:
            details = blocked(violation, "Initial call exceeds the trusted scope", {"step": index})
            self.emit("guard", "guard_result", details.result.model_dump(mode="json"))
            return incomplete(violation)
        initial = self.emit(
            "controller",
            "initial_authorization",
            {
                "step": index,
                "tool": step.tool.model_dump(mode="json"),
                "arguments": step.arguments,
                "request_key": step.request_key,
            },
        )
        points = {fault.point for fault in self.faults if fault.step == index}
        timeout: Timeout | None = None
        if "initial_before" in points:
            timeout = "before"
        elif "initial_after" in points:
            timeout = "after"
        attempt = self._dispatch(
            step.tool, step.arguments, step.request_key, None, initial.event_id, timeout
        )
        visible = attempt.visible
        checked = verify(self.gateway, step, attempt)
        self._record_verification(checked)
        if checked.status == "verified":
            self._remember_read(visible)
            return None
        needs_probe = attempt.timeout == "after" and step.request_key is not None
        while self.budget.decisions < self.budget.limits.recovery_actions:
            self.budget.decisions += 1
            proposal: bytes | Refusal | None = None
            try:
                request = self._policy_input(visible)
                self.emit("policy", "policy_input", request.model_dump(mode="json"))
                proposal = self.policy(request.model_copy(deep=True))
                if isinstance(proposal, Refusal):
                    raise ProposalError("refusal")
                if not isinstance(proposal, bytes):
                    raise ProposalError("invalid_decision")
                decision = parse_proposal(proposal, self.allowed)
            except ProposalError as exc:
                rejection: JsonObject = {"reason": str(exc), "call_id": visible.call.call_id}
                if isinstance(proposal, bytes):
                    rejection.update(size=len(proposal), digest=content_hash(proposal))
                self.emit("policy", "proposal_rejected", rejection)
                details = blocked(
                    str(exc),
                    "Policy input or proposal was rejected",
                    {"call_id": visible.call.call_id},
                )
                self.emit(
                    "guard",
                    "guard_result",
                    details.result.model_dump(mode="json") | {"source": "policy"},
                )
                return incomplete(str(exc))
            self.emit("policy", "recovery_proposal", decision.model_dump(mode="json"))
            context = GuardContext(
                step, attempt, checked, needs_probe, self.trusted_reads, self.scope, self.allowed
            )
            authorization = authorize(decision, context, self.gateway.registry, self.budget)
            guard = authorization.result
            ruling = self.emit(
                "guard", "guard_result", guard.model_dump(mode="json") | {"source": "policy"}
            )
            if guard.outcome == "block":
                return incomplete(guard.rule_id)
            if (
                guard.outcome == "confirmation_required"
                or decision.action == Action.REQUEST_INFORMATION
            ):
                return incomplete(guard.rule_id, guard.evidence)
            if decision.action == Action.STOP:
                return incomplete(guard.rule_id)
            if decision.action == Action.ACCEPT:
                self._remember_read(visible)
                return None
            candidate = authorization.candidate
            assert candidate is not None
            if decision.action == Action.VERIFY:
                probe = self._dispatch(
                    candidate.tool,
                    candidate.arguments,
                    None,
                    Action.VERIFY,
                    ruling.event_id,
                    "before" if "verification_before" in points else None,
                )
                visible = probe.visible
                checked = verify(self.gateway, step, attempt, probe)
                self._record_verification(checked)
                if checked.status == "inconclusive":
                    return incomplete("verification_inconclusive")
                needs_probe = False
            else:
                attempt = self._dispatch(
                    candidate.tool,
                    candidate.arguments,
                    candidate.request_key,
                    decision.action,
                    ruling.event_id,
                )
                visible = attempt.visible
                checked = verify(self.gateway, step, attempt)
                self._record_verification(checked)
                needs_probe = candidate.request_key is not None and step.probe is not None

        details = blocked(
            "recovery_budget_exhausted",
            "Recovery decision budget is exhausted",
            {"call_id": visible.call.call_id, "limit": self.budget.limits.recovery_actions},
        )
        self.emit("guard", "guard_result", details.result.model_dump(mode="json"))
        return incomplete(details.result.rule_id)
