"""Canonical traces and atomic publication of deterministic run evidence."""

from dataclasses import dataclass
from errno import EEXIST, ENOTEMPTY
from pathlib import Path
from tempfile import TemporaryDirectory

from toolswap.config import scoped_path
from toolswap.contracts import SCHEMA_VERSION, Event, JsonObject
from toolswap.determinism import LogicalClock, canonical_json, content_hash, stable_id


class ArtifactError(OSError):
    """Stored artifacts disagree with a fresh deterministic run."""


@dataclass(frozen=True)
class ScenarioRun:
    run_dir: Path
    passed: bool
    state_hash: str
    trace_hash: str
    infrastructure_failure: bool = False


@dataclass(frozen=True)
class TraceRecord:
    actor: str
    event_type: str
    payload: JsonObject
    state_before_hash: str
    state_after_hash: str


class TraceRecorder:
    def __init__(self, run_id: str, seed: int):
        self.run_id = run_id
        self.seed = seed
        self._clock = LogicalClock()
        self._events: list[Event] = []
        self._rows: list[bytes] = []

    @property
    def events(self) -> tuple[Event, ...]:
        return tuple(self._events)

    def append(self, record: TraceRecord) -> Event:
        sequence = len(self._events)
        fields: JsonObject = {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "event_id": stable_id("event", self.seed, self.run_id, sequence),
            "sequence": sequence,
            "actor": record.actor,
            "event_type": record.event_type,
            "payload": record.payload,
            "logical_time": self._clock.tick(),
            "state_before_hash": record.state_before_hash,
            "state_after_hash": record.state_after_hash,
            "previous_event_hash": self._events[-1].integrity_hash if sequence else None,
        }
        digest = content_hash(canonical_json(fields))
        event = Event.model_validate(fields | {"integrity_hash": digest})
        self._events.append(event)
        self._rows.append(canonical_json(event.model_dump(mode="json")) + b"\n")
        return event

    def to_bytes(self) -> bytes:
        return b"".join(self._rows)


def build_trace(run_id: str, seed: int, records: list[TraceRecord]) -> bytes:
    trace = TraceRecorder(run_id, seed)
    for record in records:
        trace.append(record)
    return trace.to_bytes()


def run_directory(root: Path, artifacts_dir: str, run_id: str) -> Path:
    _, artifact_root = scoped_path(root, artifacts_dir)
    if (artifact_root / "runs").is_symlink():
        raise ArtifactError("run storage must be a local directory")
    return artifact_root / "runs" / run_id


def _verify_artifacts(folder: Path, expected: dict[str, bytes]) -> None:
    if folder.is_symlink() or not folder.is_dir():
        raise ArtifactError("run directory is invalid")
    for relative, data in expected.items():
        path = folder / relative
        if (
            path.is_symlink()
            or path.resolve() != path
            or not path.is_file()
            or path.stat().st_size != len(data)
            or path.read_bytes() != data
        ):
            raise ArtifactError(f"stored artifact differs from replay: {relative}")
    stored = {
        path.relative_to(folder).as_posix()
        for path in folder.rglob("*")
        if not path.is_dir() or path.is_symlink()
    }
    unexpected = sorted(stored.difference(expected))
    if unexpected:
        raise ArtifactError(f"unexpected stored artifact: {unexpected[0]}")


def publish_artifacts(folder: Path, expected: dict[str, bytes]) -> None:
    if folder.exists() or folder.is_symlink():
        _verify_artifacts(folder, expected)
        return
    folder.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=folder.parent) as scratch:
        staging = Path(scratch) / "bundle"
        for relative, data in expected.items():
            path = staging / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        try:
            staging.rename(folder)
        except OSError as exc:
            if exc.errno not in {EEXIST, ENOTEMPTY}:
                raise
            _verify_artifacts(folder, expected)
