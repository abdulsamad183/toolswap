import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from toolswap.contracts import Action, RecoveryDecision, Scenario, ToolContract
from toolswap.determinism import content_hash

TOOL_FIXTURE = Path("examples/foundation/tool.yaml")


def tool_data() -> dict:
    return yaml.safe_load(TOOL_FIXTURE.read_text())


def test_tool_contract_round_trips() -> None:
    contract = ToolContract.model_validate_json(json.dumps(tool_data()))
    assert ToolContract.model_validate_json(contract.model_dump_json()) == contract


@pytest.mark.parametrize(
    "field", [name for name, info in ToolContract.model_fields.items() if info.is_required()]
)
def test_tool_contract_reports_missing_required_field(field: str) -> None:
    data = tool_data()
    data.pop(field)
    with pytest.raises(ValidationError) as error:
        ToolContract.model_validate_json(json.dumps(data))
    assert any(
        issue["loc"] == (field,) and issue["type"] == "missing" for issue in error.value.errors()
    )


@pytest.mark.parametrize(
    "update, message",
    [
        ({"input_schema": {"type": "bogus"}}, "input_schema must be a valid offline JSON Schema"),
        ({"error_schema": {"$ref": "https://example.invalid/schema"}}, "error_schema"),
        ({"side_effect": "consequential", "postconditions": []}, "postconditions"),
        ({"idempotency": "verification_required"}, "verification_tool"),
        ({"compensation_tool": {"id": "cases.cancel", "version": "1.0.0"}}, "compensation_tool"),
    ],
)
def test_tool_contract_rejects_unsafe_metadata(update: dict, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        ToolContract.model_validate_json(json.dumps(tool_data() | update))


def scenario_data() -> dict:
    return {
        "schema_version": "1.0.0",
        "scenario_id": "service_ops.case_read",
        "version": "1.0.0",
        "initial_snapshot": content_hash(b"mock snapshot"),
        "user_task": "Read the mock support case.",
        "policy_constraints": ["Read-only"],
        "allowed_tools": [{"id": "cases.get", "version": "1.0.0"}],
        "assertions": [
            {
                "schema_version": "1.0.0",
                "assertion_id": "case_open",
                "kind": "required",
                "path": ["cases", 0, "status"],
                "equals": "open",
            }
        ],
        "eligible_fault_points": [],
        "budgets": {
            "schema_version": "1.0.0",
            "retries_per_tool": 0,
            "equivalent_calls": 1,
            "recovery_actions": 0,
        },
        "seed": 0,
    }


def test_scenario_round_trip_and_bad_nested_records() -> None:
    data = scenario_data()
    scenario = Scenario.model_validate_json(json.dumps(data))
    assert scenario == Scenario.model_validate_json(scenario.model_dump_json())
    data["budgets"]["retries_per_tool"] = -1
    data["assertions"][0]["path"] = []
    data["allowed_tools"][0]["id"] = "not a tool"
    with pytest.raises(ValidationError) as error:
        Scenario.model_validate_json(json.dumps(data))
    assert {item["loc"] for item in error.value.errors()} >= {
        ("budgets", "retries_per_tool"),
        ("assertions", 0, "path"),
        ("allowed_tools", 0, "id"),
    }


@pytest.mark.parametrize("field", ["allowed_tools", "assertions"])
def test_scenario_rejects_duplicate_ids(field: str) -> None:
    data = scenario_data()
    data[field] *= 2
    with pytest.raises(ValidationError, match=field):
        Scenario.model_validate_json(json.dumps(data))


def decision_data(**changes: object) -> dict:
    return {
        "schema_version": "1.0.0",
        "verdict": "FAILURE_SUSPECTED",
        "failure_family": "side_effect_ambiguity",
        "confidence": 0.9,
        "evidence": ["The write result timed out."],
        "action": "VERIFY",
        "tool": "work_orders.get_by_idempotency_key",
        "arguments": {"key": "mock-request-1"},
        "expected_postcondition": "Work order exists.",
        "requires_confirmation": False,
    } | changes


@pytest.mark.parametrize("action", list(Action))
def test_all_eight_actions_round_trip(action: Action) -> None:
    calls = {Action.VERIFY, Action.RETRY, Action.REPAIR_ARGUMENTS, Action.SWITCH_TOOL}
    data = decision_data(action=action)
    if action not in calls:
        data.update(tool=None, arguments={})
    validated = RecoveryDecision.model_validate_json(json.dumps(data))
    assert validated.action is action
    assert RecoveryDecision.model_validate_json(validated.model_dump_json()) == validated


@pytest.mark.parametrize(
    "changes",
    [
        {"action": "EXECUTE"},
        {"action": "STOP"},
        {"tool": None, "arguments": {}},
        {"action": "STOP", "tool": None},
        {"success": True},
        {"confidence": "0.9"},
    ],
)
def test_malformed_recovery_decision_is_rejected(changes: dict) -> None:
    with pytest.raises(ValidationError):
        RecoveryDecision.model_validate_json(json.dumps(decision_data(**changes)))
