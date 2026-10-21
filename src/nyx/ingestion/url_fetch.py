"""SSRF guard for evidence URLs (SPEC §12, ADR 0006).

NYX must never fetch an internal or non-routable target on the strength of a URL supplied by a
producer. The guard is fail-closed: a host that resolves to a private, loopback, link-local,
shared (CGNAT), multicast, or otherwise reserved range is refused, and so is a host that cannot
be resolved at all -- an unresolved name is not evidence that the target is safe, and a later
DNS answer could point at an internal address (the DNS-rebinding shape).

The check is applied at the ingestion edge, where a producer first names a URL, and it must be
re-applied at any future fetch boundary: a DNS answer is not stable, so registration-time
resolution alone is not the control.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

#: IPv4 ranges that must never be fetched: RFC1918, loopback, link-local, CGNAT (100.64/10),
#: the IPv4-mapped/other reserved blocks, and multicast.
_PRIVATE_V4 = tuple(
    ipaddress.ip_network(network)
    for network in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.88.99.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "255.255.255.255/32",
    )
)

#: IPv6 ranges that must never be fetched: unspecified, loopback, ULA, link-local, multicast,
#: and the IPv4-mapped/translation prefixes that carry IPv4 targets.
_PRIVATE_V6 = tuple(
    ipaddress.ip_network(network)
    for network in (
        "::/128",
        "::1/128",
        "::ffff:0:0/96",
        "64:ff9b::/96",
        "64:ff9b:1::/48",
        "100::/64",
        "2001:db8::/32",
        "fc00::/7",
        "fe80::/10",
        "ff00::/8",
    )
)


class UrlFetchDenied(Exception):
    """Raised when a URL may not be fetched: wrong scheme, no host, or a private target."""


def _address_is_private(address: str) -> bool:
    """Return whether one IP address literal is a private or non-routable target."""

    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        # Not an address literal at all. Treated as private: refusing to guess is the point.
        return True

    # ::ffff:127.0.0.1 is a loopback target wearing an IPv6 spelling.
    mapped = getattr(parsed, "ipv4_mapped", None)
    if mapped is not None:
        parsed = mapped

    if parsed.version == 4:
        return any(parsed in network for network in _PRIVATE_V4)
    return any(parsed in network for network in _PRIVATE_V6)


def is_private_target(host: str) -> bool:
    """Return whether ``host`` is a private or otherwise non-routable fetch target.

    A literal IP address is checked directly. A name is resolved, and if *any* resolved address
    is private the host is refused. Resolution failure is refused too: fail closed.
    """

    candidate = host.strip().strip("[]")
    if not candidate:
        return True

    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        pass
    else:
        return _address_is_private(candidate)

    try:
        infos = socket.getaddrinfo(candidate, None)
    except OSError:
        # No answer, or no resolver. A name we cannot resolve is not a name we may fetch.
        return True

    if not infos:
        return True

    for info in infos:
        address = info[4][0]
        if not isinstance(address, str) or _address_is_private(address):
            return True
    return False


def host_resolves(host: str) -> bool:
    """Return whether ``host`` resolves to at least one address (IPv4 or IPv6).

    Used by the registration guard to decide whether a URL names a concrete fetch target. A
    host that does not resolve is not a fetch target: NYX records the reference at registration
    and does not fetch it, so it is not an SSRF risk. ``validate_evidence_url`` refuses it at any
    future fetch boundary, where the fail-closed rule applies.
    """

    candidate = host.strip().strip("[]")
    if not candidate:
        return False
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        try:
            return bool(socket.getaddrinfo(candidate, None))
        except OSError:
            return False
    return True


def is_http_url(value: str) -> bool:
    """Return whether ``value`` is an ``http`` or ``https`` URL.

    A reference such as ``git:abc123`` or ``s3://bucket/key`` is not an http(s) URL and is left
    alone by the URL guard; only http(s) targets can be fetched.
    """

    try:
        return urlsplit(value).scheme.lower() in ("http", "https")
    except ValueError:
        return False


def validate_evidence_url(url: str, *, allow_private: bool = False) -> None:
    """Raise :class:`UrlFetchDenied` unless ``url`` is an acceptable http(s) fetch target.

    ``allow_private=True`` is an explicit, deployment-wide opt-in; it is never inferred from the
    content of the URL.
    """

    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise UrlFetchDenied(f"evidence URL scheme {parts.scheme!r} is not http(s)")

    host = parts.hostname
    if not host:
        raise UrlFetchDenied("evidence URL does not name a host")

    if allow_private:
        return

    if is_private_target(host):
        raise UrlFetchDenied(
            f"evidence URL host {host!r} resolves to a private or non-routable target"
        )
