"""What a remote endpoint is allowed to write onto a catalog row.

A probe's ``error`` is durable: it is stored on the row, logged, and served by
``GET /api/v1/mcp/servers``. The endpoint on the other end chooses that text, so
these tests pin the two things the host decides instead -- how long it may be,
and that a credential we sent never comes back in it.
"""

from __future__ import annotations

import logging
import httpx2
import pytest

from src.server.services import mcp_probe
from src.server.services.mcp_oauth.http import OAuthHopBlocked
from src.server.services.mcp_probe import (
    PROBE_ERROR_MAX_CHARS,
    ProbeOutcome,
    _bounded,
    _describe,
    _scrub_sent_values,
    _scrub_url,
)

# A catalog row's address in the two shapes vendors ship: the key in the path
# (Zapier) and the key in the query (Smithery).
KEYED_URL = (
    "https://mcp.example.com/api/mcp/s/EXAMPLE-PATH-TOKEN/mcp"
    "?api_key=EXAMPLE-QUERY-SIG"
)


def _probe_raises(monkeypatch, error: BaseException) -> None:
    """Fail one probe at the preflight, so nothing is dialled and the error the
    probe comes home with is exactly the one given."""

    async def _pin(url, **kwargs):
        return url

    async def _preflight(url, headers):
        raise error

    monkeypatch.setattr(mcp_probe, "pin_public_url", _pin)
    monkeypatch.setattr(mcp_probe, "_preflight", _preflight)


def _assert_no_key_anywhere(outcome, caplog) -> None:
    for secret in ("EXAMPLE-PATH-TOKEN", "EXAMPLE-QUERY-SIG"):
        assert secret not in outcome.error
        assert secret not in caplog.text


def test_bounded_caps_and_marks() -> None:
    out = _bounded("x" * 5000)
    assert len(out) <= PROBE_ERROR_MAX_CHARS
    assert out.endswith("[truncated]")
    assert out.startswith("xxx")


def test_bounded_collapses_control_runs() -> None:
    assert _bounded("  first\r\n\tsecond\x00third  ") == "first second third"


def test_bounded_passes_short_text_through() -> None:
    assert _bounded("connection refused") == "connection refused"


def test_describe_bounds_a_huge_remote_message() -> None:
    # An SDK error carrying whatever JSON-RPC message the server sent; the
    # discovery client allows a response far larger than this.
    out = _describe(RuntimeError("boom " * 20_000))
    assert len(out) <= PROBE_ERROR_MAX_CHARS
    assert out.endswith("[truncated]")


def test_describe_bounds_a_connect_error() -> None:
    out = _describe(httpx2.ConnectError("nope " * 20_000))
    assert out.startswith("could not connect: ")
    assert len(out) <= PROBE_ERROR_MAX_CHARS + len("could not connect: ")
    assert out.endswith("[truncated]")


def test_scrub_redacts_a_raw_sent_value() -> None:
    out = _scrub_sent_values(
        "server answered HTTP 401: key k-123-secret is not valid",
        {"X-Api-Key": "k-123-secret"},
    )
    assert "k-123-secret" not in out
    assert "[redacted]" in out


def test_scrub_redacts_the_token_half_of_a_bearer_value() -> None:
    out = _scrub_sent_values(
        "rejected token tok-abc",
        {"Authorization": "Bearer tok-abc"},
    )
    assert "tok-abc" not in out
    assert out == "rejected token [redacted]"


def test_scrub_leaves_an_unrelated_error_alone() -> None:
    error = "could not connect: connection refused"
    assert _scrub_sent_values(error, {"X-Api-Key": "k-123-secret"}) == error


def test_scrub_ignores_empty_values() -> None:
    error = "server answered HTTP 500 to the MCP handshake"
    assert _scrub_sent_values(error, {"X-Api-Key": "", "X-Blank": "   "}) == error


@pytest.mark.asyncio
async def test_probe_remote_server_scrubs_an_echoed_credential(monkeypatch) -> None:
    async def _probe(url, headers, *, timeout_s):
        # The shape of a server that quotes the header it refused.
        return ProbeOutcome(
            ok=False,
            auth="credential",
            http_status=401,
            error=f"discovery failed: bad key {headers['X-Api-Key']}",
        )

    monkeypatch.setattr(mcp_probe, "_probe", _probe)

    outcome = await mcp_probe.probe_remote_server(
        "https://mcp.invalid/rpc", {"X-Api-Key": "k-123-secret"}
    )

    assert "k-123-secret" not in outcome.error
    assert outcome.error == "discovery failed: bad key [redacted]"
    assert outcome.sent_credential is True


@pytest.mark.asyncio
async def test_probe_scrubs_an_echoed_credential_before_logging_it(
    monkeypatch, caplog
) -> None:
    """The log line runs inside ``_probe``, before the outcome is scrubbed on
    the way out, so it has to scrub for itself."""
    secret = "EXAMPLE-OPAQUE-TOKEN-AAA"

    async def _pin(url, **kwargs):
        return url

    async def _preflight(url, headers):
        raise RuntimeError(f"401 from upstream: rejected header {secret}")

    monkeypatch.setattr(mcp_probe, "pin_public_url", _pin)
    monkeypatch.setattr(mcp_probe, "_preflight", _preflight)
    with caplog.at_level(logging.INFO, logger="src.server.services.mcp_probe"):
        outcome = await mcp_probe._probe(
            "https://example.com/mcp?sig=EXAMPLE-QUERY-SIG",
            {"X-Api-Key": secret},
            timeout_s=5,
        )

    assert secret not in caplog.text
    # The URL itself is logged as its host: a row may carry a key in its query.
    assert "EXAMPLE-QUERY-SIG" not in caplog.text
    assert "example.com" in caplog.text
    assert "[redacted]" in caplog.text
    assert secret not in outcome.error
    assert "[redacted]" in outcome.error


def test_scrub_url_cuts_a_quoted_address_back_to_its_host() -> None:
    out = _scrub_url(f"POST {KEYED_URL} answered a redirect (302)", KEYED_URL)
    assert out == "POST mcp.example.com answered a redirect (302)"


def test_scrub_url_leaves_a_message_alone_when_no_url_was_dialled() -> None:
    assert _scrub_url("connection refused", "") == "connection refused"


@pytest.mark.asyncio
async def test_probe_scrubs_a_refused_redirect_that_quotes_the_url(
    monkeypatch, caplog
) -> None:
    """``pinned_request`` refuses every 3xx and names the address it dialled in
    the refusal; that text is stored on the row and logged by discovery."""
    _probe_raises(
        monkeypatch,
        OAuthHopBlocked(
            f"POST {KEYED_URL} answered a redirect (302); "
            "redirects are refused on OAuth hops"
        ),
    )
    with caplog.at_level(logging.INFO, logger="src.server.services.mcp_probe"):
        outcome = await mcp_probe._probe(KEYED_URL, {}, timeout_s=5)

    _assert_no_key_anywhere(outcome, caplog)
    # Still diagnosable: which server, and what it did.
    assert "mcp.example.com" in outcome.error
    assert "answered a redirect (302)" in outcome.error


@pytest.mark.asyncio
async def test_probe_scrubs_an_oversized_body_refusal_that_quotes_the_url(
    monkeypatch, caplog
) -> None:
    _probe_raises(
        monkeypatch,
        OAuthHopBlocked(
            f"POST {KEYED_URL} answered more than 1048576 bytes; "
            "refusing the hop"
        ),
    )
    with caplog.at_level(logging.INFO, logger="src.server.services.mcp_probe"):
        outcome = await mcp_probe._probe(KEYED_URL, {}, timeout_s=5)

    _assert_no_key_anywhere(outcome, caplog)
    assert "mcp.example.com" in outcome.error
    assert "refusing the hop" in outcome.error


@pytest.mark.asyncio
async def test_probe_scrubs_the_url_out_of_a_library_error_it_did_not_write(
    monkeypatch, caplog
) -> None:
    """Any exception from the stack may quote the address it was given, and the
    generic branch both logs that reason and stores it."""
    _probe_raises(
        monkeypatch, RuntimeError(f"all connection attempts to {KEYED_URL} failed")
    )
    with caplog.at_level(logging.INFO, logger="src.server.services.mcp_probe"):
        outcome = await mcp_probe._probe(KEYED_URL, {}, timeout_s=5)

    _assert_no_key_anywhere(outcome, caplog)
    assert "mcp.example.com" in outcome.error
    assert "mcp.example.com" in caplog.text


@pytest.mark.asyncio
async def test_a_long_library_error_is_scrubbed_before_it_is_cut(
    monkeypatch, caplog
) -> None:
    """``_describe`` caps what it returns, so a URL sitting past the cap would
    be sliced in half and no longer match a scrub applied afterwards. Half a
    credentialled URL is still the credential."""
    # Padded so the URL straddles the 240-char cap: cut first, the tail of the
    # path token survives the slice.
    _probe_raises(
        monkeypatch,
        RuntimeError("retrying " * 17 + f"gave up on {KEYED_URL} after 5 tries"),
    )
    with caplog.at_level(logging.INFO, logger="src.server.services.mcp_probe"):
        outcome = await mcp_probe._probe(KEYED_URL, {}, timeout_s=5)

    _assert_no_key_anywhere(outcome, caplog)
    for fragment in ("EXAMPLE-PATH", "EXAMPLE-QUER", "api/mcp/s/"):
        assert fragment not in outcome.error
        assert fragment not in caplog.text
