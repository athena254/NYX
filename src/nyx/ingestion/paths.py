"""Path-traversal guard for file evidence and repository locators (SPEC §12).

A locator supplied by a producer is untrusted. Left alone it can name a file outside the data
directory (``../../etc/passwd``, ``/etc/passwd``, a UNC share, a symlink that points out of the
tree). :func:`resolve_within` resolves the candidate and verifies that the result is inside the
base directory, refusing absolute paths, ``..`` traversal segments, and symlink escapes.

The guard is deliberately structural: it does not try to recognise "bad" filenames, it checks
where the path actually lands.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath, PureWindowsPath

_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
_URI_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


class PathTraversalError(Exception):
    """Raised when a locator escapes the base directory, absolutely or via a symlink."""


def is_path_like(candidate: str) -> bool:
    """Return whether ``candidate`` names a filesystem path rather than an opaque reference.

    A URI with a scheme (``https://``, ``s3://``, ``file://``) is a reference, not a path.
    A locator such as ``job:required-integration#L1-L200`` is an opaque handle and is left alone.
    """

    if _URI_SCHEME.match(candidate):
        return False
    if _WINDOWS_DRIVE.match(candidate):
        return True
    if candidate.startswith(("/", "./", "../", "~")):
        return True
    return "/" in candidate or "\\" in candidate


def resolve_within(base: Path, candidate: str) -> Path:
    """Resolve ``candidate`` under ``base`` and return the verified path.

    Raises :class:`PathTraversalError` when the candidate is absolute, contains a ``..``
    traversal segment, starts with ``~``, or resolves (following symlinks) to a location that is
    not inside ``base``. The check runs *after* resolution, so a symlink that points outside the
    tree is caught rather than trusted.
    """

    if not candidate:
        raise PathTraversalError("locator is empty")

    if PurePosixPath(candidate).is_absolute() or PureWindowsPath(candidate).is_absolute():
        raise PathTraversalError("absolute locator is not permitted")
    if _WINDOWS_DRIVE.match(candidate):
        raise PathTraversalError("absolute locator is not permitted")
    if candidate.startswith("~"):
        raise PathTraversalError("home-relative locator is not permitted")

    segments = set(PurePosixPath(candidate).parts) | set(PureWindowsPath(candidate).parts)
    if ".." in segments:
        raise PathTraversalError("locator contains a '..' traversal segment")

    base_resolved = base.expanduser().resolve()
    resolved = (base_resolved / candidate).resolve()

    if resolved != base_resolved and base_resolved not in resolved.parents:
        raise PathTraversalError("locator resolves outside the data directory")

    return resolved
