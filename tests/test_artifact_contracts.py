import json

import pytest
from pydantic import ValidationError

from toolswap.contracts import (
    AssertionResult,
    EffectRecord,
    Event,
    GuardResult,
    MetricReport,
    ReproducibilityManifest,
    StateReference,
)
from toolswap.determinism import canonical_json, content_hash, stable_id

VERSION = {"schema_version": "1.0.0"}
RUN_ID = stable_id("run", 0, "service_ops.case_read")
STATE_HASH = content_hash(b"mock snapshot")
ASSERTION = VERSION | {
    "assertion_id": "visible",
    "kind": "required",
    "passed": True,
    "evidence": {},
}
EVENT = VERSION | {
    "run_id": RUN_ID,
    "event_id": stable_id("event", 0, RUN_ID, 0),
    "sequence": 0,
    "actor": "environment",
    "event_type": "reset",
    "payload": {},
    "logical_time": 0,
    "state_after_hash": STATE_HASH,
}


@pytest.mark.parametrize(
    "record_type, fixture",
    [
        (StateReference, VERSION | {"state_hash": STATE_HASH}),
        (AssertionResult, ASSERTION),
        (Event, EVENT | {"integrity_hash": content_hash(canonical_json(EVENT))}),
        (
            EffectRecord,
            VERSION
            | {
                "effect_id": stable_id("effect", 0, RUN_ID),
                "tool": {"id": "work_orders.create", "version": "1.1.0"},
                "request_key": "mock-request-1",
                "status": "complete",
                "state_before_hash": STATE_HASH,
                "state_after_hash": content_hash(b"updated mock snapshot"),
                "result": {},
            },
        ),
        (
            GuardResult,
            VERSION
            | {
                "outcome": "block",
                "rule_id": "authority",
                "reason": "Out of scope",
                "evidence": {},
            },
        ),
        (
            MetricReport,
            VERSION
            | {
                "run_id": RUN_ID,
                "task_success": True,
                "safety_adjusted_success": True,
                "unsafe_proposals": 0,
                "unsafe_executions": 0,
                "duplicate_effects": 0,
                "false_success": False,
                "false_interventions": 0,
                "assertions": [ASSERTION],
            },
        ),
        (
            ReproducibilityManifest,
            VERSION
            | {
                "code_revision": "example-commit",
                "code_dirty": False,
                "lockfile_hash": content_hash(b"lockfile"),
                "config_hash": content_hash(b"run config"),
                "scenario": {"id": "service_ops.case_read", "version": "1.0.0"},
                "seed": 0,
                "python_version": "3.12.15",
                "platform": "macOS arm64",
            },
        ),
    ],
)
def test_artifact_records_round_trip_and_require_a_supported_version(record_type, fixture) -> None:
    record = record_type.model_validate_json(json.dumps(fixture))
    assert record_type.model_validate_json(record.model_dump_json()) == record
    with pytest.raises(ValidationError, match="schema_version"):
        record_type.model_validate_json(json.dumps(fixture | {"schema_version": "2.0.0"}))
