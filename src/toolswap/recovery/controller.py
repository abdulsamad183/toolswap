"""Bounded recovery transitions for one trusted sequence of tool operations."""

from dataclasses import dataclass, field
from typing import Protocol

from toolswap.contracts import SCHEMA_VERSION, Action, JsonObject, Reference, Scenario
from toolswap.determinism import canonical_json
from toolswap.recovery.gateway import Dispatch, Gateway, Timeout
from toolswap.recovery.guard import BudgetLedger, GuardContext, authorize, blocked
from toolswap.recovery.records import Fault, Observation, PolicyInput, Refusal, Scope, Step
from toolswap.recovery.verifier import verify
from toolswap.replay import TraceRecord
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
    records: list[TraceRecord] = field(default_factory=list, init=False)
    timings_ns: dict[str, int] = field(default_factory=dict, init=False)
    trusted_reads: dict[Reference, Observation] = field(default_factory=dict, init=False)
    history: list[Observation] = field(default_factory=list, init=False)
    budget: BudgetLedger = field(init=False)
    allowed: dict[str, Reference] = field(init=False)

    def __post_init__(self) -> None:
        self.budget = BudgetLedger(self.scenario.budgets)
        self.allowed = {ref.id: ref for ref in self.scenario.allowed_tools}

    def emit(self, actor: str, event_type: str, payload: JsonObject) -> None:
        state = self.gateway.store.current_state().state_hash
        self.records.append(TraceRecord(actor, event_type, payload, state, state))

    def _dispatch(
        self,
        tool: Reference,
        arguments: JsonObject,
        key: str | None,
        action: Action | None,
        timeout: Timeout | None = None,
    ) -> Dispatch:
        self.budget.dispatch(tool, arguments, action)
        outcome = self.gateway.execute(tool, arguments, request_key=key, timeout=timeout)
        self.timings_ns[outcome.visible.call.call_id] = outcome.elapsed_ns
        self.history.append(outcome.visible)
        del self.history[:-HISTORY_LIMIT]
        effect = outcome.actual.effect if outcome.actual is not None else None
        self.records.append(
            TraceRecord(
                "gateway",
                "tool_result",
                {
                    "visible": outcome.visible.model_dump(mode="json"),
                    "entered_environment": outcome.actual is not None,
                    "effect_id": effect.effect_id if effect is not None else None,
                    "fault": outcome.timeout,
                },
                outcome.before,
                outcome.after,
            )
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

    def run(self, steps: tuple[Step, ...], claims: JsonObject) -> JsonObject:
        self.emit("environment", "snapshot_loaded", {"scenario": self.scenario.scenario_id})
        self.emit(
            "controller",
            "run_started",
            {
                "budgets": self.scenario.budgets.model_dump(mode="json"),
                "scope": self.scope.model_dump(mode="json"),
                "eligible_fault_points": list(self.scenario.eligible_fault_points),
            },
        )
        for index, step in enumerate(steps):
            incomplete = self._run_step(index, step)
            if incomplete is not None:
                return incomplete
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
        points = {fault.point for fault in self.faults if fault.step == index}
        timeout: Timeout | None = None
        if "initial_before" in points:
            timeout = "before"
        elif "initial_after" in points:
            timeout = "after"
        attempt = self._dispatch(step.tool, step.arguments, step.request_key, None, timeout)
        visible = attempt.visible
        checked = verify(self.gateway, step, attempt)
        self.emit("verifier", "verification", checked.model_dump(mode="json"))
        if checked.status == "verified":
            self._remember_read(visible)
            return None
        needs_probe = attempt.timeout == "after" and step.request_key is not None
        while self.budget.decisions < self.budget.limits.recovery_actions:
            self.budget.decisions += 1
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
                details = blocked(
                    str(exc),
                    "Policy input or proposal was rejected",
                    {"call_id": visible.call.call_id},
                )
                self.emit("guard", "guard_result", details.result.model_dump(mode="json"))
                return incomplete(str(exc))
            self.emit("policy", "recovery_proposal", decision.model_dump(mode="json"))
            context = GuardContext(
                step, attempt, checked, needs_probe, self.trusted_reads, self.scope, self.allowed
            )
            authorization = authorize(decision, context, self.gateway.registry, self.budget)
            guard = authorization.result
            self.emit("guard", "guard_result", guard.model_dump(mode="json"))
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
                    "before" if "verification_before" in points else None,
                )
                visible = probe.visible
                checked = verify(self.gateway, step, attempt, probe)
                self.emit("verifier", "verification", checked.model_dump(mode="json"))
                if checked.status == "inconclusive":
                    return incomplete("verification_inconclusive")
                needs_probe = False
            else:
                attempt = self._dispatch(
                    candidate.tool, candidate.arguments, candidate.request_key, decision.action
                )
                visible = attempt.visible
                checked = verify(self.gateway, step, attempt)
                self.emit("verifier", "verification", checked.model_dump(mode="json"))
                needs_probe = candidate.request_key is not None and step.probe is not None

        details = blocked(
            "recovery_budget_exhausted",
            "Recovery decision budget is exhausted",
            {"call_id": visible.call.call_id, "limit": self.budget.limits.recovery_actions},
        )
        self.emit("guard", "guard_result", details.result.model_dump(mode="json"))
        return incomplete(details.result.rule_id)
