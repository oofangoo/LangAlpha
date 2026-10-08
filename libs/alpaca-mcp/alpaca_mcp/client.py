"""The Alpaca REST client, bound to the paper trading host.

There is deliberately no setting that selects another host. A live key sent to
the paper host is refused by Alpaca, so the account this server can move is the
paper account whatever credentials it is handed.

Credentials ride in per call and are never stored: the server holds no secret of
its own, so reaching it gives nothing that the caller's own keys did not.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

PAPER_TRADING_URL = "https://paper-api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"

KEY_ID_HEADER = "apca-api-key-id"
SECRET_HEADER = "apca-api-secret-key"

# Optional operator-supplied keys, used only for a request that carries none.
ENV_KEY_ID = "ALPACA_API_KEY_ID"
ENV_SECRET = "ALPACA_API_SECRET_KEY"

Base = Literal["trading", "data"]

# Alpaca's own limit is 200 requests a minute per account. A call that waits
# longer than this is more likely stuck than slow.
REQUEST_TIMEOUT_S = 15.0


class MissingCredentials(Exception):
    """The caller sent no Alpaca key pair."""


class UpstreamUnavailable(Exception):
    """Alpaca did not answer, so nothing is known about what it did.

    Distinct from :class:`AlpacaError` on purpose: a refusal is Alpaca's word,
    while this is the absence of one, and for an order the two mean opposite
    things. A refused order does not exist; an unanswered one may.
    """


class AlpacaError(Exception):
    """Alpaca answered with an error, in its own words.

    ``code`` is Alpaca's numeric error code and ``message`` its text. Both are
    passed on untouched, because the order adapter on the other side recognises
    a vendor refusal by exactly this shape.
    """

    def __init__(self, status: int, code: int | None, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message

    def to_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {"message": self.message, "http_status": self.status}
        if self.code is not None:
            body["code"] = self.code
        return body

    def __str__(self) -> str:
        return json.dumps(self.to_body())


@dataclass(frozen=True)
class Credentials:
    key_id: str
    secret: str = field(repr=False)

    @classmethod
    def from_headers(
        cls, headers: Any, default: Credentials | None = None
    ) -> Credentials:
        """The key pair a request carried, else the operator's, else a refusal.

        The pair is taken whole from one source. A request that names a key id
        but no secret is not completed from the environment: that would send one
        account's id with another's secret, or quietly spend the operator's
        account on a call that meant to use somebody else's.
        """
        key_id = ((headers.get(KEY_ID_HEADER) if headers else None) or "").strip()
        secret = ((headers.get(SECRET_HEADER) if headers else None) or "").strip()
        if key_id and secret:
            return cls(key_id=key_id, secret=secret)
        if not key_id and not secret and default is not None:
            return default
        raise MissingCredentials(
            "no Alpaca paper credentials were sent: the connection must carry "
            "APCA-API-KEY-ID and APCA-API-SECRET-KEY"
        )

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> Credentials | None:
        """The operator's key pair, or None unless both halves are set."""
        env = os.environ if environ is None else environ
        key_id = (env.get(ENV_KEY_ID) or "").strip()
        secret = (env.get(ENV_SECRET) or "").strip()
        return cls(key_id=key_id, secret=secret) if key_id and secret else None


class AlpacaClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    @classmethod
    def create(cls) -> AlpacaClient:
        return cls(
            httpx.AsyncClient(
                timeout=httpx.Timeout(REQUEST_TIMEOUT_S),
                follow_redirects=False,
                trust_env=False,
            )
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def request(
        self,
        method: str,
        path: str,
        creds: Credentials,
        *,
        base: Base = "trading",
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        url = (PAPER_TRADING_URL if base == "trading" else DATA_URL) + path
        try:
            response = await self._http.request(
                method,
                url,
                params={k: v for k, v in (params or {}).items() if v is not None},
                json=body,
                headers={
                    "APCA-API-KEY-ID": creds.key_id,
                    "APCA-API-SECRET-KEY": creds.secret,
                    "accept": "application/json",
                },
            )
        except httpx.TimeoutException as e:
            raise UpstreamUnavailable("Alpaca did not answer in time") from e
        except httpx.HTTPError as e:
            # The type and not the message: a protocol error can quote the
            # header it choked on, and every header here is a credential.
            raise UpstreamUnavailable(
                f"could not reach Alpaca ({type(e).__name__})"
            ) from e
        if response.is_success:
            if response.status_code == 204 or not response.content:
                return {}
            try:
                return response.json()
            except ValueError as e:
                raise UpstreamUnavailable("Alpaca sent a body that is not JSON") from e
        raise _error_from(response)


def _error_from(response: httpx.Response) -> AlpacaError:
    code: int | None = None
    message = response.reason_phrase or "request failed"
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        raw_code = payload.get("code")
        if isinstance(raw_code, int) and not isinstance(raw_code, bool):
            code = raw_code
        raw_message = payload.get("message")
        if isinstance(raw_message, str) and raw_message.strip():
            message = raw_message.strip()
    return AlpacaError(response.status_code, code, message)
