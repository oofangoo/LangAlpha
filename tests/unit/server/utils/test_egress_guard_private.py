"""The operator's private allowance: one named origin, and nothing around it.

The guard exists to keep a user-supplied URL from reaching the network the server
runs on. The allowance is the single, deliberate hole in it, so these tests are
mostly about what it refuses: a sibling port, a different scheme, a listing that
is wider than an origin, and an address the listed name should never resolve to.
"""

from __future__ import annotations

import pytest

from src.server.models.mcp_server import validate_remote_url
from src.server.utils.egress_guard import (
    OPERATOR_PRIVATE_ENV,
    EgressBlockedError,
    _operator_private_ip_ok,
    is_operator_private_destination,
    operator_private_destinations,
    pin_public_url,
)

SIDECAR = "http://10.1.2.3:8765"


@pytest.fixture
def allow(monkeypatch):
    def _set(value: str) -> None:
        monkeypatch.setenv(OPERATOR_PRIVATE_ENV, value)

    return _set


def test_nothing_is_allowed_by_default(monkeypatch):
    monkeypatch.delenv(OPERATOR_PRIVATE_ENV, raising=False)
    assert operator_private_destinations() == frozenset()
    assert not is_operator_private_destination(f"{SIDECAR}/mcp")


@pytest.mark.parametrize(
    "entry",
    [
        "http://svc",  # no port
        "http://svc:8765/path",  # a path widens it
        "http://user:pw@svc:8765",  # a credential
        "http://*.internal:8765",  # a wildcard
        "http://10.0.0.0/8:8765",  # not an origin
        "ftp://svc:21",  # not http(s)
        "svc:8765",  # no scheme
        "http://svc:8765?x=1",
    ],
)
def test_a_listing_wider_than_an_origin_is_refused(allow, entry):
    allow(entry)
    assert operator_private_destinations() == frozenset()


def test_several_origins_and_blanks(allow):
    allow(" http://alpaca-mcp:8765 , ,https://Other.Internal:9443/ ")
    assert operator_private_destinations() == frozenset(
        {("http", "alpaca-mcp", 8765), ("https", "other.internal", 9443)}
    )


def test_match_is_exact_on_scheme_host_and_port(allow):
    allow(SIDECAR)
    assert is_operator_private_destination(f"{SIDECAR}/mcp")
    assert is_operator_private_destination("http://10.1.2.3:8765/anything?q=1")
    assert not is_operator_private_destination("http://10.1.2.3:8766/mcp")
    assert not is_operator_private_destination("http://10.1.2.4:8765/mcp")
    assert not is_operator_private_destination("https://10.1.2.3:8765/mcp")
    assert not is_operator_private_destination("http://10.1.2.3.evil.com:8765/mcp")
    assert not is_operator_private_destination("http://u:p@10.1.2.3:8765/mcp")
    assert not is_operator_private_destination("not a url")


def test_default_ports_are_filled_in(allow):
    allow("https://svc.internal:443")
    assert is_operator_private_destination("https://svc.internal/mcp")


@pytest.mark.parametrize(
    "ip,host,ok",
    [
        ("10.1.2.3", "alpaca-mcp", True),
        ("172.18.0.5", "alpaca-mcp", True),
        ("192.168.1.9", "alpaca-mcp", True),
        ("169.254.169.254", "alpaca-mcp", False),  # cloud metadata
        ("127.0.0.1", "alpaca-mcp", False),  # a service name rebinding to loopback
        ("127.0.0.1", "127.0.0.1", True),  # the operator listed loopback
        ("127.0.0.1", "localhost", True),
        ("0.0.0.0", "alpaca-mcp", False),
        ("224.0.0.1", "alpaca-mcp", False),
        ("93.184.216.34", "alpaca-mcp", False),  # public: not what was vouched for
        ("fe80::1", "alpaca-mcp", False),
    ],
)
def test_what_a_listed_name_may_resolve_to(ip, host, ok):
    assert _operator_private_ip_ok(ip, host) is ok


@pytest.mark.asyncio
async def test_without_the_flag_a_listed_origin_is_still_blocked(allow):
    allow(SIDECAR)
    with pytest.raises(EgressBlockedError):
        await pin_public_url(f"{SIDECAR}/mcp")
    with pytest.raises(EgressBlockedError):
        await pin_public_url(f"{SIDECAR}/mcp", require_https=False)


@pytest.mark.asyncio
async def test_with_the_flag_the_listed_origin_is_pinned_over_plain_http(allow):
    allow(SIDECAR)
    target = await pin_public_url(f"{SIDECAR}/mcp", allow_operator_private=True)
    assert target.ip == "10.1.2.3"
    assert target.url == "http://10.1.2.3:8765/mcp"
    assert target.authority == "10.1.2.3:8765"


@pytest.mark.asyncio
async def test_the_flag_covers_only_the_listed_origin(allow):
    allow(SIDECAR)
    for url in (
        "http://10.1.2.3:9999/mcp",  # sibling port
        "http://10.1.2.4:8765/mcp",  # sibling host
        "http://127.0.0.1:8765/mcp",  # loopback
        "http://169.254.169.254/latest/meta-data",
        "http://10.1.2.3:8765@evil.example/mcp",
    ):
        with pytest.raises(EgressBlockedError):
            await pin_public_url(url, allow_operator_private=True)


@pytest.mark.asyncio
async def test_the_flag_does_nothing_when_nothing_is_listed(monkeypatch):
    monkeypatch.delenv(OPERATOR_PRIVATE_ENV, raising=False)
    with pytest.raises(EgressBlockedError):
        await pin_public_url(f"{SIDECAR}/mcp", allow_operator_private=True)


@pytest.mark.asyncio
async def test_a_listed_name_that_resolves_to_metadata_is_refused(allow, monkeypatch):
    allow("http://alpaca-mcp:8765")

    async def rebinding(host, *, port=443, allow_non_global=False):
        return ["169.254.169.254"]

    monkeypatch.setattr(
        "src.server.utils.egress_guard.resolve_public_ips", rebinding
    )
    with pytest.raises(EgressBlockedError, match="does not cover"):
        await pin_public_url("http://alpaca-mcp:8765/mcp", allow_operator_private=True)


@pytest.mark.asyncio
async def test_public_urls_are_unaffected(allow):
    allow(SIDECAR)
    target = await pin_public_url("https://93.184.216.34/mcp", allow_operator_private=True)
    assert target.ip == "93.184.216.34"
    with pytest.raises(EgressBlockedError):
        await pin_public_url("http://93.184.216.34/mcp", allow_operator_private=True)


def test_write_time_validator_accepts_only_the_listed_private_origin(allow, monkeypatch):
    monkeypatch.delenv(OPERATOR_PRIVATE_ENV, raising=False)
    with pytest.raises(ValueError):
        validate_remote_url(f"{SIDECAR}/mcp")

    allow(SIDECAR)
    assert validate_remote_url(f"{SIDECAR}/mcp") == f"{SIDECAR}/mcp"
    for url in (
        "http://10.1.2.3:9999/mcp",
        "http://10.1.2.4:8765/mcp",
        "http://u:p@10.1.2.3:8765/mcp",
        "http://localhost:8765/mcp",
    ):
        with pytest.raises(ValueError):
            validate_remote_url(url)
    # Public servers keep their rules.
    assert validate_remote_url("https://example.com/mcp")
    with pytest.raises(ValueError, match="https"):
        validate_remote_url("http://example.com/mcp")
