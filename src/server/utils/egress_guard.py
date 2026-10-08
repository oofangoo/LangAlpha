"""Resolution-time SSRF guard for host-originated egress to user-configured URLs.

``validate_remote_url`` (models/mcp_server.py) is the static write-time policy;
it documents DNS rebinding as an accepted residual because the sandbox is the
caller. Host-side egress (OAuth discovery/DCR/token hops, the egress relay's
vendor dial) has no such excuse: this module resolves the host itself, rejects
any non-global address, and pins the connection to a validated IP — the URL is
rewritten to the IP while TLS SNI and the Host header keep the original
hostname, so a rebinding resolver cannot swap the target between check and
connect.
"""

from __future__ import annotations

import asyncio
import functools
import ipaddress
import logging
import os
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

__all__ = [
    "EgressBlockedError",
    "MCP_PROTOCOL_HEADERS",
    "PinnedTarget",
    "RESERVED_HEADERS",
    "RESERVED_REQUEST_HEADERS",
    "is_operator_private_destination",
    "operator_private_destinations",
    "pin_public_url",
    "resolve_public_ips",
    "strip_configured_headers",
    "strip_reserved_headers",
]

# Headers no caller may supply: framing and hop-by-hop fields belong to the
# client that frames the request, and ``Host`` is the pin itself. A dict is
# case-sensitive where HTTP is not, so a lowercase ``host`` from a config row
# would ride alongside the pinned one and put two on the wire.
RESERVED_REQUEST_HEADERS = frozenset({
    "host",
    "content-length",
    "transfer-encoding",
    "connection",
    "te",
    "upgrade",
    "expect",
    "content-encoding",
})


# Names the MCP protocol owns, on top of the framing ones. Whoever negotiated
# the session frames these, never configuration: a row spelling the version
# desyncs the wire header from the body ``_meta`` it has to match, and one
# spelling a session id forges a session the server never issued.
MCP_PROTOCOL_HEADERS = frozenset({
    "mcp-protocol-version",
    "mcp-method",
    "mcp-name",
    "mcp-session-id",
})

# What a user-configured header map may never carry. The host probe, the egress
# relay and the sandbox runtime each merge a row's headers into a request they
# framed themselves, so all three drop exactly this set. Split any of them and
# the same row probes green on one path and fails on another.
RESERVED_HEADERS = RESERVED_REQUEST_HEADERS | MCP_PROTOCOL_HEADERS


def strip_reserved_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    """Drop every :data:`RESERVED_REQUEST_HEADERS` key, matched case-folded.

    The transport's own strip, for a map it is about to re-frame. A caller
    holding a *configured* map wants :func:`strip_configured_headers` instead:
    the protocol names this one keeps are ones the transport itself is
    entitled to send.
    """
    return {
        k: v
        for k, v in (headers or {}).items()
        if k.lower() not in RESERVED_REQUEST_HEADERS
    }


def strip_configured_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    """Drop every :data:`RESERVED_HEADERS` key, matched case-folded."""
    return {
        k: v
        for k, v in (headers or {}).items()
        if k.lower() not in RESERVED_HEADERS
    }


class EgressBlockedError(ValueError):
    """The target host failed egress policy (scheme, resolution, or IP range)."""


@dataclass(frozen=True)
class PinnedTarget:
    """A URL rewritten to a validated IP, plus what the transport must restore.

    ``url`` carries the IP in the netloc; ``host`` is the original hostname used
    as the TLS ``sni_hostname`` extension so certificate verification still runs
    against the real name. ``authority`` is what the caller must send as the
    ``Host`` header — the hostname plus a non-default port (bracketed for IPv6),
    since a server routing or validating the full authority rejects a bare host.
    """

    url: str
    host: str
    ip: str
    authority: str

    def pinned_kwargs(
        self, headers: Mapping[str, str] | None = None
    ) -> tuple[str, dict[str, str], dict[str, str]]:
        """The (url, headers, extensions) triple that keeps the pin intact.

        Every caller must apply all three together — sending the pinned URL
        without the restored Host/SNI reaches the right IP under the wrong
        name, and sending the original URL re-resolves the hostname.
        """
        sent = strip_reserved_headers(headers)
        sent["Host"] = self.authority
        return self.url, sent, {"sni_hostname": self.host}


def _classify(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    # is_global covers private/loopback/link-local/reserved/multicast/
    # unspecified AND CGNAT — same predicate as the write-time validator.
    return ip.is_global


async def resolve_public_ips(
    host: str,
    *,
    port: int = 443,
    allow_non_global: bool = False,
) -> list[str]:
    """Resolve ``host`` and return its addresses, rejecting non-global ones.

    Every resolved address must pass — a name that maps to one public and one
    private address is an attack shape, not a configuration.
    """
    candidate = host.lower().rstrip(".").strip("[]")
    try:
        literal = ipaddress.ip_address(candidate)
    except ValueError:
        literal = None
    if literal is not None:
        if not allow_non_global and not _classify(literal):
            raise EgressBlockedError(
                f"egress to {host!r} is blocked: non-global address"
            )
        return [str(literal)]

    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(
            candidate, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
        )
    except OSError as exc:
        raise EgressBlockedError(f"egress to {host!r} is blocked: DNS resolution failed") from exc
    ips: list[str] = []
    for _family, _type, _proto, _canon, sockaddr in infos:
        addr = ipaddress.ip_address(sockaddr[0])
        if not allow_non_global and not _classify(addr):
            raise EgressBlockedError(
                f"egress to {host!r} is blocked: resolves to a non-global address"
            )
        if str(addr) not in ips:
            ips.append(str(addr))
    if not ips:
        raise EgressBlockedError(f"egress to {host!r} is blocked: no addresses resolved")
    return ips


logger = logging.getLogger(__name__)

#: Comma-separated ``scheme://host:port`` entries, set by the operator.
OPERATOR_PRIVATE_ENV = "EGRESS_PRIVATE_ALLOWLIST"


@functools.lru_cache(maxsize=8)
def _parse_private_allowlist(raw: str) -> frozenset[tuple[str, str, int]]:
    """``(scheme, host, port)`` for each well-formed entry; the rest are dropped.

    An entry has to be exactly an origin: a scheme, a host and an explicit port,
    nothing else. A path, a credential, a wildcard or a CIDR would each widen
    what one line of configuration means, so they are refused rather than
    interpreted, and refused loudly because the operator meant something by them.
    """
    allowed: set[tuple[str, str, int]] = set()
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            parts = urlsplit(entry)
            port = parts.port
        except ValueError:
            port = None
            parts = None
        host = parts.hostname if parts else None
        if (
            parts is None
            or parts.scheme not in ("http", "https")
            or not host
            or port is None
            or parts.username
            or parts.password
            or parts.path not in ("", "/")
            or parts.query
            or parts.fragment
            or "*" in host
        ):
            logger.warning(
                "[egress_guard] ignoring %s entry %r: it must be exactly "
                "scheme://host:port",
                OPERATOR_PRIVATE_ENV,
                entry,
            )
            continue
        allowed.add((parts.scheme, host.lower().rstrip("."), port))
    return frozenset(allowed)


def operator_private_destinations() -> frozenset[tuple[str, str, int]]:
    """The private origins the operator has allowed, read at call time.

    Read from the environment on every call and not at import, so that a test
    or an operator's restart sees exactly what is set now. Empty unless set,
    which is the default: with nothing listed this module behaves as it always
    has.
    """
    return _parse_private_allowlist(os.getenv(OPERATOR_PRIVATE_ENV, ""))


def _origin_of(url: str) -> tuple[str, str, int] | None:
    try:
        parts = urlsplit(url)
        port = parts.port or {"http": 80, "https": 443}.get(parts.scheme)
    except ValueError:
        return None
    if not parts.hostname or port is None:
        return None
    return parts.scheme, parts.hostname.lower().rstrip("."), port


def is_operator_private_destination(url: str) -> bool:
    """Whether ``url`` is, to the scheme, host and port, one the operator listed.

    Exact on purpose. A listed origin is permission to reach one named service,
    never a class of addresses, so a sibling port, a sibling host or a different
    scheme on the same machine is not covered. Userinfo is refused outright:
    nothing about reaching an internal service calls for a credential in its URL.
    """
    allowed = operator_private_destinations()
    if not allowed:
        return False
    try:
        parts = urlsplit(url)
        if parts.username or parts.password:
            return False
    except ValueError:
        return False
    return _origin_of(url) in allowed


def _operator_private_ip_ok(ip_text: str, allowed_host: str) -> bool:
    """Whether a listed host may resolve to this address.

    The listing vouches for a name, not for whatever that name later points at.
    A service on the stack's own network resolves to a private address, so that
    is what is accepted. Link-local space is refused outright because it is where
    cloud metadata lives, and loopback only when the operator listed a loopback
    host, since a service name that suddenly resolves to 127.0.0.1 is a
    rebinding, not a configuration.
    """
    ip = ipaddress.ip_address(ip_text)
    if ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        return False
    if ip.is_loopback:
        host = allowed_host.strip("[]")
        if host == "localhost":
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False
    return ip.is_private


async def pin_public_url(
    url: str,
    *,
    allow_non_global: bool = False,
    require_https: bool = True,
    allow_operator_private: bool = False,
) -> PinnedTarget:
    """Validate ``url`` and return it pinned to one validated resolved IP.

    Callers send the request with ``PinnedTarget.pinned_kwargs()``, which
    carries the pinned URL, the restored Host authority and the SNI extension.

    ``allow_operator_private`` is for the callers that dial a server the
    operator deployed beside this one and named in ``EGRESS_PRIVATE_ALLOWLIST``.
    It lifts both the https and the global-address requirements for that exact
    origin and for nothing else, and the resolved address is still checked, so
    it is a narrower thing than ``allow_non_global``. A caller that handles a
    URL a third party supplied must never pass it.
    """
    exempt = allow_operator_private and is_operator_private_destination(url)
    try:
        parts = urlsplit(url)
    except ValueError as e:
        # The url is third-party in every caller (a manifest field, a
        # handshake icon, an upstream Location), so unparseable is an ordinary
        # input, not a fault. ``http://[bad`` raises here rather than
        # returning something to reject.
        raise EgressBlockedError(f"egress url is not parseable: {e}") from e
    if require_https and parts.scheme != "https" and not exempt:
        raise EgressBlockedError("egress requires https")
    if parts.scheme not in ("https", "http"):
        raise EgressBlockedError(f"egress scheme {parts.scheme!r} is not allowed")
    if parts.username or parts.password:
        raise EgressBlockedError("egress url must not contain userinfo credentials")
    host = parts.hostname
    if not host:
        raise EgressBlockedError("egress url must include a host")

    default_port = 443 if parts.scheme == "https" else 80
    try:
        port = parts.port or default_port
    except ValueError as e:
        # ``parts.port`` parses lazily, so a port that is out of range or not
        # a number survives urlsplit and raises on this read instead.
        raise EgressBlockedError(f"egress url has an unusable port: {e}") from e
    ips = await resolve_public_ips(
        host, port=port, allow_non_global=allow_non_global or exempt
    )
    if exempt:
        bad = [i for i in ips if not _operator_private_ip_ok(i, host.lower())]
        if bad:
            raise EgressBlockedError(
                f"egress to {host!r} is blocked: it resolves to an address the "
                "private allowance does not cover"
            )
    ip = ips[0]

    ip_netloc = f"[{ip}]" if ":" in ip else ip
    if parts.port is not None:
        ip_netloc = f"{ip_netloc}:{parts.port}"
    pinned = urlunsplit((parts.scheme, ip_netloc, parts.path, parts.query, ""))
    # The Host authority keeps the original hostname (bracketed for IPv6) and
    # carries a non-default port; SNI/cert verification still use the bare host.
    host_netloc = f"[{host}]" if ":" in host else host
    authority = (
        f"{host_netloc}:{parts.port}"
        if parts.port is not None and parts.port != default_port
        else host_netloc
    )
    return PinnedTarget(url=pinned, host=host, ip=ip, authority=authority)
