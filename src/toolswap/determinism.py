"""Canonical JSON and seed-derived identities for replayable fixtures."""

import hashlib
import json
import random
from dataclasses import dataclass

from pydantic import JsonValue


def canonical_json(value: JsonValue) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def content_hash(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def stable_id(namespace: str, seed: int, *parts: JsonValue) -> str:
    digest = hashlib.sha256(canonical_json([namespace, seed, *parts])).hexdigest()
    return f"{namespace}_{digest}"


def seeded_random(seed: int, stream: str) -> random.Random:
    return random.Random(stable_id("rng", seed, stream))


@dataclass
class LogicalClock:
    _time: int = 0

    def tick(self) -> int:
        current = self._time
        self._time += 1
        return current
