"""Seal and verify recovery artifacts without running the environment."""

from pathlib import Path
from typing import Final, Literal

from pydantic import ValidationError

from toolswap.config import InputError
from toolswap.contracts import (
    SCHEMA_VERSION,
    ContentHash,
    Count,
    EffectRecord,
    Event,
    Identifier,
    MetricReport,
    ResolvedRunManifest,
    StateReference,
    VersionedRecord,
)
from toolswap.determinism import canonical_json, content_hash, stable_id
from toolswap.recovery.evaluation import dispatch_is_authorized, verified_steps
from toolswap.recovery.records import Observation, Termination, ToolCall, Verification
from toolswap.replay import ArtifactError
from toolswap.state_snapshots import decode_snapshot

ARTIFACT_FORMAT: Final = "2.0.0"
SEALED_FILES = frozenset(
    {
        "effects.jsonl",
        "metrics.json",
        "resolved_config.json",
        "response.json",
        "state_refs.json",
        "termination.json",
        "trace.jsonl",
    }
)


class IntegrityManifest(VersionedRecord):
    schema_version: Literal["1.0.0"] = SCHEMA_VERSION
    artifact_format: Literal["2.0.0"] = ARTIFACT_FORMAT
    run_id: Identifier
    event_count: Count
    final_event_hash: ContentHash
    files: dict[str, ContentHash]


def record_line(record: VersionedRecord) -> bytes:
    return canonical_json(record.model_dump(mode="json")) + b"\n"


def seal_bundle(events: tuple[Event, ...], artifacts: dict[str, bytes]) -> bytes:
    final = events[-1]
    return record_line(
        IntegrityManifest(
            run_id=final.run_id,
            event_count=len(events),
            final_event_hash=final.integrity_hash,
            files={path: content_hash(data) for path, data in artifacts.items()},
        )
    )


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ArtifactError(message)


def _read(folder: Path, relative: str) -> bytes:
    path = folder / relative
    _require(
        path.resolve() == path and not path.is_symlink() and path.is_file(),
        f"missing or unsafe artifact: {relative}",
    )
    return path.read_bytes()


def _record[T: VersionedRecord](cls: type[T], data: bytes) -> T:
    record = cls.model_validate_json(data)
    _require(record_line(record) == data, f"{cls.__name__} is not canonical JSON")
    return record


def _rows[T: VersionedRecord](cls: type[T], data: bytes) -> list[T]:
    return [_record(cls, line) for line in data.splitlines(keepends=True)]


def _check_chain(
    events: list[Event], seal: IntegrityManifest, manifest: ResolvedRunManifest
) -> set[str]:
    _require(len(events) == seal.event_count and bool(events), "event count differs from seal")
    previous_hash = None
    state = events[0].state_before_hash
    states: set[str] = set()
    for index, event in enumerate(events):
        before, after = event.state_before_hash, event.state_after_hash
        _require(
            event.run_id == seal.run_id
            and event.sequence == event.logical_time == index
            and event.event_id == stable_id("event", manifest.seed, seal.run_id, index)
            and event.previous_event_hash == previous_hash
            and content_hash(
                canonical_json(event.model_dump(mode="json", exclude={"integrity_hash"}))
            )
            == event.integrity_hash,
            f"broken event chain at sequence {index}",
        )
        if before is None or after is None or before != state:
            raise ArtifactError(f"broken state chain at sequence {index}")
        if before != after:
            _require(
                event.event_type in {"tool_result", "tool_interrupted"},
                f"unexpected state change at sequence {index}",
            )
        states.update((before, after))
        previous_hash, state = event.integrity_hash, after
    _require(previous_hash == seal.final_event_hash, "final event hash differs from seal")
    return states


def _check_call_links(
    events: list[Event], effects: list[EffectRecord], seed: int, infrastructure: bool
) -> None:
    by_id = {event.event_id: event for event in events}
    ledger = {effect.effect_id: effect for effect in effects}
    attempts: dict[str, Event] = {}
    dispatched: set[str] = set()
    results: set[str] = set()
    authorizations: set[str] = set()
    linked_effects: list[str] = []
    recorded_effects: list[str] = []
    for event in events:
        details = event.payload
        if event.event_type in {"tool_attempt", "tool_dispatch"}:
            call = ToolCall.model_validate(details.get("call"))
            ref = details.get("authorization_id")
            authorization = by_id.get(ref) if isinstance(ref, str) else None
            _require(
                dispatch_is_authorized(event, call, authorization), "missing call authorization"
            )
            if event.event_type == "tool_attempt":
                _require(
                    call.call_id == stable_id("call", seed, event.run_id, len(attempts)),
                    "invalid call identifier",
                )
                attempts[call.call_id] = event
            else:
                attempt = attempts.get(call.call_id)
                _require(
                    isinstance(ref, str)
                    and ref not in authorizations
                    and attempt is not None
                    and attempt.payload == details
                    and call.call_id not in dispatched
                    and call.call_id not in results
                    and attempt.state_after_hash == event.state_before_hash,
                    "dispatch differs from its authorized attempt",
                )
                assert isinstance(ref, str)
                authorizations.add(ref)
                dispatched.add(call.call_id)
        elif event.event_type in {"tool_result", "tool_interrupted"}:
            if event.event_type == "tool_result":
                call = Observation.model_validate(details.get("visible")).call
                entered = details.get("entered_environment")
            else:
                call = ToolCall.model_validate(details.get("call"))
                entered = True
                _require(infrastructure, "interrupted call requires an infrastructure outcome")
            attempt = attempts.get(call.call_id)
            _require(
                attempt is not None
                and attempt.payload["call"] == call.model_dump(mode="json")
                and call.call_id not in results
                and entered is (call.call_id in dispatched)
                and attempt.state_after_hash == event.state_before_hash,
                "result differs from its tool attempt",
            )
            results.add(call.call_id)
            effect_id = details.get("effect_id")
            if effect_id is None:
                _require(event.state_before_hash == event.state_after_hash, "unlinked state change")
                continue
            effect = ledger.get(effect_id) if isinstance(effect_id, str) else None
            if (
                effect is None
                or not entered
                or (effect.tool, effect.request_key) != (call.tool, call.request_key)
            ):
                raise ArtifactError("result references an unrelated ledger effect")
            if effect.effect_id in linked_effects:
                _require(
                    effect.status == "partial"
                    and event.state_before_hash == event.state_after_hash,
                    "ledger effect is linked to multiple writes",
                )
            else:
                _require(
                    (effect.state_before_hash, effect.state_after_hash)
                    == (event.state_before_hash, event.state_after_hash),
                    "ledger and write snapshots differ",
                )
                linked_effects.append(effect.effect_id)
        elif event.event_type == "effect_recorded":
            effect = EffectRecord.model_validate(details)
            _require(
                ledger.get(effect.effect_id) == effect
                and effect.effect_id in linked_effects
                and effect.state_after_hash == event.state_after_hash,
                "environment event differs from its ledger effect",
            )
            recorded_effects.append(effect.effect_id)
        elif event.event_type == "verification":
            check = Verification.model_validate_json(
                canonical_json({k: v for k, v in details.items() if k != "step"})
            )
            _require(
                check.call_id in results
                and (check.probe_call_id is None or check.probe_call_id in results)
                and check.state_hash == event.state_after_hash
                and (
                    "effect_id" not in check.evidence
                    or check.evidence["effect_id"] in linked_effects
                ),
                "verification references missing call or effect evidence",
            )
    _require(
        linked_effects == recorded_effects == [effect.effect_id for effect in effects],
        "effect ledger differs from the trace",
    )
    _require(infrastructure or results == attempts.keys(), "tool attempt has no result")


def _check_outcome(
    events: list[Event], artifacts: dict[str, bytes], effects: list[EffectRecord], seed: int
) -> None:
    final = events[-1]
    terminal = _record(Termination, artifacts["termination.json"])
    state = _record(StateReference, artifacts["state_refs.json"])
    metrics = _record(MetricReport, artifacts["metrics.json"])
    kinds = [event.event_type for event in events]
    infrastructure = terminal.kind == "infrastructure_failure"
    _require(
        kinds[:3] == ["snapshot_loaded", "run_started", "user_task"]
        and len(events) >= 6
        and kinds.count("terminated") == 1
        and kinds[-3:] == ["final_response", "graded", "terminated"]
        and final.payload == terminal.model_dump(mode="json")
        and final.state_after_hash == terminal.state_hash == state.state_hash
        and terminal.last_evidence_event_id == events[-4].event_id
        and canonical_json(events[-3].payload) + b"\n" == artifacts["response.json"]
        and events[-2].payload == metrics.model_dump(mode="json")
        and metrics.run_id == final.run_id,
        "terminal, response, or evaluator evidence does not match trace",
    )
    _require(
        events[1].payload.get("manifest_hash") == content_hash(artifacts["resolved_config.json"]),
        "resolved manifest differs from the trace",
    )
    count = events[1].payload.get("step_count")
    _require(
        isinstance(count, int)
        and not isinstance(count, bool)
        and count >= 0
        and (terminal.kind != "completed" or verified_steps(events) == list(range(count))),
        "completed run is missing verified steps",
    )
    _check_call_links(events, effects, seed, infrastructure)


def verify_bundle(folder: Path, expected_trace_hash: str | None = None) -> str:
    """Return the hash of an intact recovery trace or raise an artifact error."""
    folder = folder.absolute()
    try:
        _require(folder.is_dir() and folder.resolve() == folder, "invalid run directory")
        seal = _record(IntegrityManifest, _read(folder, "integrity.json"))
        artifacts = {name: _read(folder, name) for name in sorted(SEALED_FILES)}
        trace_hash = content_hash(artifacts["trace.jsonl"])
        _require(expected_trace_hash in (None, trace_hash), "trace differs from expected hash")
        for name, data in artifacts.items():
            _require(seal.files.get(name) == content_hash(data), f"artifact digest differs: {name}")

        events = _rows(Event, artifacts["trace.jsonl"])
        manifest = _record(ResolvedRunManifest, artifacts["resolved_config.json"])
        states = _check_chain(events, seal, manifest)
        for state_hash in sorted(states):
            name = f"states/{state_hash.removeprefix('sha256:')}.json"
            data = _read(folder, name)
            _require(
                seal.files.get(name) == content_hash(data) == state_hash, f"state differs: {name}"
            )
            decode_snapshot(data)
            artifacts[name] = data
        paths = {
            path.relative_to(folder).as_posix()
            for path in folder.rglob("*")
            if path.is_symlink() or not path.is_dir()
        }
        _require(
            seal.files.keys() == artifacts.keys()
            and paths == artifacts.keys() | {"integrity.json"},
            "unexpected artifact inventory",
        )
        _check_outcome(
            events, artifacts, _rows(EffectRecord, artifacts["effects.jsonl"]), manifest.seed
        )
    except (InputError, ValidationError, ValueError, RuntimeError) as exc:
        raise ArtifactError("invalid recovery evidence") from exc
    return trace_hash
