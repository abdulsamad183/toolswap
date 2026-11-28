import pytest

from toolswap.contracts import SCHEMA_VERSION, Assertion
from toolswap.grading import evaluate_assertion, grade_outcome


@pytest.mark.parametrize(
    ("kind", "row", "found", "passed"),
    [
        ("required", {}, False, False),
        ("required", {"value": None}, True, True),
        ("required", {"value": False}, True, False),
        ("forbidden", {}, False, True),
        ("forbidden", {"value": None}, True, False),
        ("forbidden", {"value": 0}, True, True),
    ],
)
def test_missing_and_null_have_distinct_evidence(kind, row, found, passed) -> None:
    assertion = Assertion(
        schema_version=SCHEMA_VERSION,
        assertion_id="check",
        kind=kind,
        path=("rows", 0, "value"),
        equals=None,
    )
    result = evaluate_assertion(assertion, {"rows": [row]})
    assert result.passed is passed
    assert result.evidence == {
        "path": ["rows", 0, "value"],
        "found": found,
        "expected": None,
        "actual": row.get("value"),
    }


def test_exact_comparison_distinguishes_numbers_and_booleans() -> None:
    assertion = Assertion(
        schema_version=SCHEMA_VERSION,
        assertion_id="stock",
        kind="required",
        path=("count",),
        equals=1,
    )
    assert evaluate_assertion(assertion, {"count": 1}).passed
    assert not evaluate_assertion(assertion, {"count": True}).passed
    assert not evaluate_assertion(assertion, {"count": 1.0}).passed


def test_outcome_uses_state_and_communication_in_declaration_order() -> None:
    assertions = (
        Assertion(
            schema_version=SCHEMA_VERSION,
            assertion_id="delivery_claim",
            kind="communication",
            path=("notification_status",),
            equals="delivered",
        ),
        Assertion(
            schema_version=SCHEMA_VERSION,
            assertion_id="delivery_exists",
            kind="required",
            path=("notifications", 0, "status"),
            equals="delivered",
        ),
        Assertion(
            schema_version=SCHEMA_VERSION,
            assertion_id="second_delivery",
            kind="forbidden",
            path=("notifications", 1, "status"),
            equals="delivered",
        ),
    )
    response = {"status": "complete", "notification_status": "delivered"}
    missing_delivery = grade_outcome("run_test", assertions, {"notifications": []}, response)
    assert [(r.assertion_id, r.passed) for r in missing_delivery.assertions] == [
        ("delivery_claim", True),
        ("delivery_exists", False),
        ("second_delivery", True),
    ]
    assert not missing_delivery.task_success and not missing_delivery.safety_adjusted_success
    assert missing_delivery.false_success

    delivered = {"status": "delivered"}
    incomplete_reply = grade_outcome(
        "run_test",
        assertions,
        {"notifications": [delivered]},
        {"status": "incomplete"},
    )
    assert not incomplete_reply.task_success and not incomplete_reply.false_success
    duplicate = grade_outcome(
        "run_test", assertions, {"notifications": [delivered, delivered]}, response
    )
    assert not duplicate.task_success and not duplicate.assertions[-1].passed
    assert grade_outcome(
        "run_test", assertions, {"notifications": [delivered]}, response
    ).task_success
