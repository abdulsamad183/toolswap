import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from toolswap.determinism import (
    LogicalClock,
    canonical_json,
    content_hash,
    seeded_random,
    stable_id,
)


@given(st.dictionaries(st.text(), st.integers(), max_size=15))
def test_order_does_not_change_serialization_or_hash(values: dict[str, int]) -> None:
    reversed_values = dict(reversed(list(values.items())))
    assert canonical_json(values) == canonical_json(reversed_values)
    assert content_hash(canonical_json(values)) == content_hash(canonical_json(reversed_values))


def test_seeded_ids_clock_and_random_stream() -> None:
    def events() -> list[tuple[int, str, int]]:
        clock = LogicalClock()
        rng = seeded_random(42, "fixture")
        return [
            (clock.tick(), stable_id("event", 42, index), rng.randrange(100)) for index in range(4)
        ]

    assert events() == events()
    assert [event[0] for event in events()] == [0, 1, 2, 3]
    assert len({event[1] for event in events()}) == 4
    assert seeded_random(42, "fixture").getstate() != seeded_random(42, "fault").getstate()
    assert stable_id("event", 42, 0) != stable_id("event", 43, 0)


def test_canonical_json_rejects_nonfinite_numbers() -> None:
    with pytest.raises(ValueError):
        canonical_json({"bad": math.nan})
