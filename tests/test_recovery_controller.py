import json
from pathlib import Path

import pytest

from toolswap.config import parse_yaml_record
from toolswap.contracts import SCHEMA_VERSION, Action, Budgets, Reference, Scenario
from toolswap.determinism import canonical_json
from toolswap.recovery.controller import Controller
from toolswap.recovery.gateway import Gateway
from toolswap.recovery.guard import scope_violation
from toolswap.recovery.records import Alternative, Fault, Refusal, Scope, Step
from toolswap.recovery.scenarios import PREVIEWS, _proposal
from toolswap.recovery.verifier import verify
from toolswap.scenario import FIXTURES
from toolswap.service_contracts import tool_reference
from toolswap.service_operations import build_service_registry
from toolswap.state import StateStore
from toolswap.trust import MAX_PROPOSAL_BYTES

READ = tool_reference("accounts.get")
FALLBACK = tool_reference("accounts.verify")
ARGS = {"account_id": "mock-account-1"}
READ_STEP = Step(tool=READ, arguments=ARGS, expected=ARGS | {"verified": True})
READ_SCOPE = Scope(
    permissions=("account.read",), resources=ARGS, max_targets=1, effect_ceiling="none"
)
TRANSIENT = (Fault(step=0, point="initial_before"),)
M2 = PREVIEWS[tool_reference("service_ops.committed_timeout")]
M3 = PREVIEWS[tool_reference("service_ops.schema_repair")]


def _wire(action, tool=None, arguments=None):
    return canonical_json(_proposal(action, tool, arguments).model_dump(mode="json"))


def _controller(
    root: Path,
    steps,
    scripted=(),
    *,
    scope=READ_SCOPE,
    faults=TRANSIENT,
    limits=(1, 2, 4),
    policy=None,
    revised_case=False,
) -> Controller:
    base = parse_yaml_record(FIXTURES / "state_safety.yaml", Scenario)
    allowed = dict.fromkeys(step.tool for step in steps)
    allowed.update(dict.fromkeys(step.probe.tool for step in steps if step.probe is not None))
    for step in steps:
        allowed.update(dict.fromkeys(alt.tool for alt in step.alternatives))
    scenario = base.model_copy(
        update={
            "allowed_tools": tuple(allowed),
            "budgets": Budgets(
                schema_version=SCHEMA_VERSION,
                retries_per_tool=limits[0],
                equivalent_calls=limits[1],
                recovery_actions=limits[2],
            ),
        }
    )
    snapshot = canonical_json(json.loads((FIXTURES / "state_safety_snapshot.json").read_bytes()))
    store = StateStore.reset(root / "state.sqlite3", scenario, snapshot)
    gateway = Gateway(
        store, build_service_registry(include_revised_case=revised_case), "run_test", 0
    )
    script = iter(scripted)
    return Controller(gateway, scenario, scope, policy or (lambda request: next(script)), faults)


def _tool_results(controller):
    return [event.payload for event in controller.records if event.event_type == "tool_result"]


def test_transient_read_retries_then_accepts_verified_result(tmp_path):
    controller = _controller(
        tmp_path, (READ_STEP,), (_wire(Action.RETRY, READ, ARGS), _wire(Action.ACCEPT))
    )
    reply = controller.run((READ_STEP,), {})
    assert reply["status"] == "complete"
    assert [r["entered_environment"] for r in _tool_results(controller)] == [False, True]
    assert not controller.gateway.store.effects()
    assert controller.budget.retries[READ] == 1 and controller.budget.decisions == 2
    assert len(controller.timings_ns) == 2 and all(t >= 0 for t in controller.timings_ns.values())
    assert not any("elapsed_ns" in r for r in _tool_results(controller))


@pytest.mark.parametrize(
    ("limits", "reason", "decisions"),
    [
        ((1, 1, 4), "equivalent_call_budget_exhausted", 1),
        ((0, 2, 4), "retry_budget_exhausted", 1),
        ((1, 2, 0), "recovery_budget_exhausted", 0),
    ],
)
def test_budgets_stop_before_an_extra_call(tmp_path, limits, reason, decisions):
    controller = _controller(
        tmp_path, (READ_STEP,), (_wire(Action.RETRY, READ, ARGS),), limits=limits
    )
    assert controller.run((READ_STEP,), {})["reason"] == reason
    assert controller.budget.decisions == decisions
    assert len(_tool_results(controller)) == 1
    assert not controller.gateway.store.effects()


def test_equivalent_call_budget_ignores_write_key(tmp_path):
    create = M2.steps[2]
    second = create.model_copy(update={"request_key": "different-key"})
    controller = _controller(
        tmp_path, (create, second), scope=M2.scope, faults=(), limits=(1, 1, 4)
    )
    assert controller.run((create, second), {})["reason"] == "equivalent_call_budget_exhausted"
    assert len(_tool_results(controller)) == len(controller.gateway.store.effects()) == 1


@pytest.mark.parametrize(
    ("action", "reason"),
    [
        (Action.RETRY, "retry_requires_no_effect"),
        (Action.ACCEPT, "accept_requires_verification"),
    ],
)
def test_ambiguous_write_blocks_retry_and_accept(tmp_path, action, reason):
    create = M2.steps[2]
    proposal = (
        _wire(action, create.tool, create.arguments) if action == Action.RETRY else _wire(action)
    )
    controller = _controller(
        tmp_path,
        (create,),
        (proposal,),
        scope=M2.scope,
        faults=(Fault(step=0, point="initial_after"),),
    )
    assert controller.run((create,), {})["reason"] == reason
    assert len(_tool_results(controller)) == len(controller.gateway.store.effects()) == 1


def test_write_retry_needs_positive_no_effect_and_reuses_key(tmp_path):
    create = M2.steps[2]
    probe = create.probe
    assert probe is not None
    script = (
        _wire(Action.VERIFY, probe.tool, probe.arguments),
        _wire(Action.RETRY, create.tool, create.arguments),
        _wire(Action.VERIFY, probe.tool, probe.arguments),
        _wire(Action.ACCEPT),
    )
    controller = _controller(tmp_path, (create,), script, scope=M2.scope)
    assert controller.run((create,), {})["status"] == "complete"
    initial, absent, retry, lookup = _tool_results(controller)
    assert not initial["entered_environment"] and retry["entered_environment"]
    assert absent["visible"]["error"]["code"] == "not_found"
    assert any(
        event.payload.get("status") == "not_applied"
        and event.payload.get("probe_call_id") == absent["visible"]["call"]["call_id"]
        for event in controller.records
    )
    assert initial["visible"]["call"]["request_key"] == retry["visible"]["call"]["request_key"]
    assert lookup["visible"]["call"]["tool"]["id"] == "work_orders.get_by_idempotency_key"
    assert len(controller.gateway.store.effects()) == 1


def test_verification_cannot_change_the_original_key(tmp_path):
    create = M2.steps[2]
    probe = create.probe
    assert probe is not None
    controller = _controller(
        tmp_path,
        (create,),
        (_wire(Action.VERIFY, probe.tool, {"idempotency_key": "other"}),),
        scope=M2.scope,
        faults=(Fault(step=0, point="initial_after"),),
    )
    assert controller.run((create,), {})["reason"] == "unsafe_verification_path"
    assert len(_tool_results(controller)) == len(controller.gateway.store.effects()) == 1


def test_confirmation_states_the_exact_consequential_effect(tmp_path):
    create = M2.steps[2]
    scope = M2.scope.model_copy(update={"confirmation_tools": (create.tool,)})
    controller = _controller(
        tmp_path, (create,), (_wire(Action.REQUIRE_CONFIRMATION),), scope=scope
    )
    reply = controller.run((create,), {})
    assert reply["reason"] == "confirmation_required"
    assert reply["handoff"]["arguments"] == create.arguments
    assert reply["handoff"]["proposed_effect"] == "consequential"
    assert reply["handoff"]["request_key"] == create.request_key
    assert not controller.gateway.store.effects()


@pytest.mark.parametrize(
    ("action", "reason", "handoff"),
    [
        (Action.REQUEST_INFORMATION, "information_required", True),
        (Action.REQUIRE_CONFIRMATION, "confirmation_required", True),
        (Action.STOP, "policy_stop", False),
    ],
)
def test_escalations_and_stop_end_without_mutation(tmp_path, action, reason, handoff):
    scope = READ_SCOPE.model_copy(update={"confirmation_tools": (READ,)})
    controller = _controller(tmp_path, (READ_STEP,), (_wire(action),), scope=scope)
    reply = controller.run((READ_STEP,), {})
    assert reply["reason"] == reason and reply["unresolved_step"] == 0
    assert ("handoff" in reply) is handoff
    if action == Action.REQUIRE_CONFIRMATION:
        assert reply["handoff"]["proposed_effect"] == "none"
        assert reply["handoff"]["arguments"] == ARGS
    assert len(_tool_results(controller)) == 1 and not controller.gateway.store.effects()


@pytest.mark.parametrize(
    ("fresh", "capability", "reason"),
    [
        (True, "account_lookup", None),
        (False, "account_lookup", "incompatible_fallback"),
        (True, "unrelated_capability", "incompatible_fallback"),
    ],
)
def test_switch_checks_pinned_capability_and_freshness(tmp_path, fresh, capability, reason):
    alt = Alternative(tool=FALLBACK, arguments=ARGS, capability=capability, fresh=fresh)
    step = READ_STEP.model_copy(update={"capability": "account_lookup", "alternatives": (alt,)})
    controller = _controller(
        tmp_path, (step,), (_wire(Action.SWITCH_TOOL, FALLBACK, ARGS), _wire(Action.ACCEPT))
    )
    output = controller.run((step,), {})
    if reason is None:
        assert output["status"] == "complete" and len(_tool_results(controller)) == 2
    else:
        assert output["reason"] == reason and len(_tool_results(controller)) == 1
    assert not controller.gateway.store.effects()


@pytest.mark.parametrize(
    ("tool", "permissions", "effect", "args", "expected"),
    [
        ("cases.get", ("account.read",), "none", {"case_id": "mock-case-1"}, "permission_widening"),
        ("cases.get", ("case.read",), "none", {"case_id": "mock-case-2"}, "resource_widening"),
        (
            "inventory.reserve",
            ("inventory.write",),
            "reversible",
            {"quantity": 2},
            "target_count_widening",
        ),
        ("work_orders.create", ("case_replacement_approved",), "reversible", {}, "effect_widening"),
    ],
)
def test_authority_checks_permission_resources_count_and_effect(
    tool, permissions, effect, args, expected
):
    registry = build_service_registry()
    scope = Scope(
        permissions=permissions,
        resources={"case_id": "mock-case-1"},
        max_targets=1,
        effect_ceiling=effect,
    )
    assert scope_violation(scope, registry.contract(tool_reference(tool)), args) == expected


def test_scope_blocks_fallback_before_dispatch(tmp_path):
    tool = tool_reference("cases.get")
    args = {"case_id": "mock-case-1"}
    alt = Alternative(tool=tool, arguments=args, capability="case_lookup", fresh=True)
    step = READ_STEP.model_copy(update={"capability": "case_lookup", "alternatives": (alt,)})
    controller = _controller(tmp_path, (step,), (_wire(Action.SWITCH_TOOL, tool, args),))
    assert controller.run((step,), {})["reason"] == "permission_widening"
    assert len(_tool_results(controller)) == 1


@pytest.mark.parametrize(
    ("proposal", "reason"),
    [
        (b"invalid input", "malformed_json"),
        (b"{}", "invalid_decision"),
        (_wire(Action.RETRY, Reference(id="undeclared", version="1.0.0"), ARGS), "undeclared_tool"),
        (b" " * (MAX_PROPOSAL_BYTES + 1), "overlong_output"),
        (Refusal(), "refusal"),
    ],
)
def test_policy_boundary_classifies_invalid_output(tmp_path, proposal, reason):
    controller = _controller(tmp_path, (READ_STEP,), (proposal,))
    assert controller.run((READ_STEP,), {})["reason"] == reason
    assert len(_tool_results(controller)) == 1 and not controller.gateway.store.effects()
    assert controller.records[-1].payload["outcome"] == "block"


def test_schema_invalid_repair_is_classified_before_dispatch(tmp_path):
    update = M3.steps[1].tool
    controller = _controller(
        tmp_path,
        M3.steps,
        (_wire(Action.REPAIR_ARGUMENTS, update, {}),),
        scope=M3.scope,
        faults=(),
        revised_case=True,
    )
    assert controller.run(M3.steps, {})["reason"] == "invalid_arguments"
    assert len(_tool_results(controller)) == 2 and not controller.gateway.store.effects()


def test_policy_input_mutations_cannot_turn_into_trusted_provenance(tmp_path):
    good = M3.script[0]

    def policy(request):
        request.history[-1].result["notes"] = "fabricated"
        return _wire(
            Action.REPAIR_ARGUMENTS,
            M3.steps[1].tool,
            good.arguments | {"expected_notes": "fabricated"},
        )

    controller = _controller(
        tmp_path, M3.steps, scope=M3.scope, faults=(), revised_case=True, policy=policy
    )
    assert controller.run(M3.steps, {})["reason"] == "argument_without_provenance"
    assert not controller.gateway.store.effects()


def test_verifier_rejects_contradictory_and_missing_write_evidence(tmp_path):
    create = M2.steps[2]
    controller = _controller(tmp_path, (create,), scope=M2.scope, faults=())
    gateway = controller.gateway
    outcome = gateway.execute(create.tool, create.arguments, request_key=create.request_key)
    assert verify(gateway, create, outcome).status == "verified"
    wrong = create.model_copy(update={"expected": {"status": "cancelled"}})
    bad_row = create.model_copy(update={"expected_row": {"request_key": "invalid"}})
    missing = create.model_copy(update={"expected_row": None})
    assert verify(gateway, wrong, outcome).evidence["reason"] == "observation_mismatch"
    assert verify(gateway, bad_row, outcome).evidence["reason"] == "state_evidence_mismatch"
    assert verify(gateway, missing, outcome).evidence["reason"] == "effect_evidence_mismatch"
