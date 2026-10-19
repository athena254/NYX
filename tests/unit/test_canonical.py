"""Canonical JSON and content-hash tests."""

from __future__ import annotations

import re
from random import Random
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from nyx.domain.canonical import canonical_json, canonical_text, content_hash

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def _shuffle_keys(value: Any, rng: Random) -> Any:
    if isinstance(value, dict):
        items = list(value.items())
        rng.shuffle(items)
        return {key: _shuffle_keys(child, rng) for key, child in items}
    if isinstance(value, list):
        return [_shuffle_keys(child, rng) for child in value]
    return value


def test_canonical_json_is_stable_under_key_reordering() -> None:
    left = {"b": 2, "a": {"z": [1, 2, {"y": True, "x": None}], "m": "value"}}
    right = {"a": {"m": "value", "z": [1, 2, {"x": None, "y": True}]}, "b": 2}

    assert canonical_json(left) == canonical_json(right)
    assert canonical_text(left) == '{"a":{"m":"value","z":[1,2,{"x":null,"y":true}]},"b":2}'


def test_hash_format_and_utf8_encoding() -> None:
    digest = content_hash({"message": "naïve — 日本語"})

    assert _DIGEST.match(digest)
    assert canonical_text({"message": "naïve — 日本語"}) == '{"message":"naïve — 日本語"}'


def test_different_payloads_hash_differently() -> None:
    assert content_hash({"a": 1}) != content_hash({"a": 2})
    assert content_hash({"a": 1}) != content_hash({"a": "1"})


json_scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(),
)
nested_json = st.recursive(
    json_scalars,
    lambda children: st.one_of(
        st.lists(children, max_size=5),
        st.dictionaries(st.text(), children, max_size=5),
    ),
    max_leaves=25,
)


@given(nested_json, st.randoms())
def test_canonical_json_is_invariant_under_random_key_shuffle(payload: Any, rng: Random) -> None:
    shuffled = _shuffle_keys(payload, rng)

    assert canonical_json(payload) == canonical_json(shuffled)
    assert content_hash(payload) == content_hash(shuffled)
    assert _DIGEST.match(content_hash(shuffled))
