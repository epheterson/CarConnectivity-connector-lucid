"""One authenticated LucidAPI, kept alive by a stored refresh token.

Measured 2026-09-03 on a Gravity, not assumed:
  * a refreshed session's id_token (the gRPC bearer) lasts 300 s and the server
    enforces it to the second -- a call 45 s past expiry is UNAUTHENTICATED;
  * the refresh token is opaque and does not rotate, so re-reading the same
    file forever is safe and the mount can be read-only;
  * back-to-back refreshes return the SAME session until it is near expiry, so
    refreshing more often than needed is pure waste -- and GetNewJWTToken is
    rate-limited (RESOURCE_EXHAUSTED), which a naive client hit 3,691 times in
    24 days.

So: refresh only when under REFRESH_MARGIN_S remain, and never on every poll.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Callable, Optional

from carconnectivity.errors import AuthenticationError, RetrievalError, TemporaryAuthenticationError, TooManyRequestsError

LOG = logging.getLogger("carconnectivity.connectors.lucid.auth")

REFRESH_MARGIN_S = 60.0  # must exceed the poll interval; 60 s min interval -> refresh has 2x margin on a 300 s token


def load_refresh_token(path: Path) -> str:
    """Read the opaque refresh token from a JSON file."""
    try:
        return json.loads(path.read_text())["refresh_token"]
    except (OSError, ValueError, KeyError) as exc:
        raise AuthenticationError(f"No Lucid refresh token at {path} (expected JSON with a 'refresh_token' key)") from exc


def translate_api_error(exc: BaseException) -> Exception:
    """Map lucidmotors/gRPC failures onto CarConnectivity's error taxonomy so the
    connector loop backs off correctly (TooManyRequests -> 15 min, temporary auth
    -> next interval)."""
    code = getattr(exc, "code", None)
    name = getattr(code, "name", str(code))
    if name == "RESOURCE_EXHAUSTED":
        return TooManyRequestsError(f"Lucid rate limit: {exc}")
    if name in ("UNAUTHENTICATED", "PERMISSION_DENIED"):
        return TemporaryAuthenticationError(f"Lucid auth rejected: {exc}")
    if name in ("UNAVAILABLE", "DEADLINE_EXCEEDED", "INTERNAL"):
        return RetrievalError(f"Lucid API unavailable: {exc}")
    return RetrievalError(f"Lucid API error: {exc}")


class LucidSession:
    """Owns the asyncio loop and the LucidAPI. Everything is called from the
    connector's background thread via `run()`."""

    def __init__(self, token_path: Path, api_factory: Optional[Callable[[], Any]] = None) -> None:
        self._token_path = token_path
        self._api_factory = api_factory
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._api: Any = None

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        """Create the event loop; call from the thread that will own it."""
        self._loop = asyncio.new_event_loop()

    def close(self) -> None:
        """Close the API and the loop."""
        if self._loop is None:
            return
        if self._api is not None:
            try:
                self._loop.run_until_complete(self._api.close())
            except Exception:  # pylint: disable=broad-except
                LOG.debug("error closing Lucid API", exc_info=True)
            self._api = None
        self._loop.close()
        self._loop = None

    def run(self, coro):
        """Run a coroutine on this session's loop."""
        if self._loop is None:
            raise RuntimeError("LucidSession.start() not called")
        return self._loop.run_until_complete(coro)

    # -- auth ----------------------------------------------------------------
    def _make_api(self):
        if self._api_factory is not None:
            return self._api_factory()
        from lucidmotors import LucidAPI  # pylint: disable=import-outside-toplevel
        return LucidAPI()

    async def _ensure(self):
        if self._api is None:
            api = self._make_api()
            if hasattr(api, "__aenter__"):
                # The client stays open across many polls, so its lifetime is ours, not a with-block's.
                await api.__aenter__()  # pylint: disable=unnecessary-dunder-call
            token = load_refresh_token(self._token_path)
            try:
                await api.login_with_refresh_token(token)
            except Exception as exc:  # pylint: disable=broad-except
                raise translate_api_error(exc) from exc
            self._api = api
            LOG.info("Lucid session established")
        remaining = self._api.session_time_remaining.total_seconds()
        if remaining < REFRESH_MARGIN_S:
            LOG.debug("refreshing Lucid session (%.0fs remaining)", remaining)
            try:
                await self._api.authentication_refresh()
            except Exception as exc:  # pylint: disable=broad-except
                # Do not rebuild-and-retry here: that is the burst the rate limit exists to stop.
                raise translate_api_error(exc) from exc
        return self._api

    def api(self):
        """Authenticated client, refreshed only when it needs to be."""
        return self.run(self._ensure())

    def fetch_vehicles(self):
        """All vehicles with full state -- one gRPC call."""
        async def _go():
            api = await self._ensure()
            try:
                return await api.fetch_vehicles()
            except Exception as exc:  # pylint: disable=broad-except
                raise translate_api_error(exc) from exc
        return self.run(_go())
