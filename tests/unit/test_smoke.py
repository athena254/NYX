"""Smoke test: the package imports and the test harness runs."""

import nyx


def test_package_imports() -> None:
    assert nyx.__doc__ is None or isinstance(nyx.__doc__, str)
