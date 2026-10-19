"""Property test: malformed/random JSON never raises an unhandled exception (SPEC §17.1).

The ingestion pipeline must always produce a typed result — accepted (``created``/``duplicate``)
or ``rejected`` with a structured error — for any input, however malformed.
"""

from __future__ import annotations

from typing import Any

from hypothesis import HealthCheck, given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st

from nyx.ingestion.errors import IngestResult
from nyx.ingestion.service import IngestionService

_json_scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10**9), max_value=10**9),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=50),
)
_json = st.recursive(
    _json_scalars,
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(st.text(max_size=12), children, max_size=4),
    ),
    max_leaves=12,
)


@hypothesis_settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(payload=_json)
def test_random_json_is_always_handled(service: IngestionService, payload: Any) -> None:
    result = service.ingest_event(payload, request_id="req-property")

    assert isinstance(result, IngestResult)
    assert result.status in {"created", "duplicate", "rejected"}
    if result.status == "rejected":
        assert result.error is not None
        assert result.error.code


@hypothesis_settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(payload=st.binary(max_size=200))
def test_random_bytes_are_always_handled(service: IngestionService, payload: bytes) -> None:
    result = service.ingest_event(payload, request_id="req-property-bytes")

    assert isinstance(result, IngestResult)
    assert result.status in {"created", "duplicate", "rejected"}
