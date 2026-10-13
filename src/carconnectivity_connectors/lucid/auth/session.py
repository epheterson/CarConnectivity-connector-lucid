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
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from carconnectivity.errors import AuthenticationError, RetrievalError, TemporaryAuthenticationError, TooManyRequestsError

LOG = logging.getLogger("carconnectivity.connectors.lucid.auth")

REFRESH_MARGIN_S = 60.0  # must exceed the poll interval; 60 s min interval -> refresh has 2x margin on a 300 s token
WAKE_SETTLE_S = 8.0  # a car that was asleep needs a moment after WakeupVehicle before it takes a command
# The library sets no deadline on a command. Without one a call Lucid never answers holds
# the session lock, and the poller with it, for good. It covers the whole command, login included.
COMMAND_TIMEOUT_S = 60.0


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
    """Owns the asyncio loop and the LucidAPI. The poller calls it from the connector's
    background thread; commands arrive on other threads (the REST or MQTT plugin's). Both
    go through `run()`, which serialises them: a loop cannot run two coroutines from two
    threads at once, and a second login is exactly what must not happen."""

    def __init__(self, token_path: Path, api_factory: Optional[Callable[[], Any]] = None) -> None:
        self._token_path = token_path
        self._api_factory = api_factory
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._api: Any = None
        self._lock = threading.RLock()
        self._wake_settle_s = WAKE_SETTLE_S
        self._command_timeout_s = COMMAND_TIMEOUT_S

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        """Create the event loop; call from the thread that will own it."""
        with self._lock:
            self._loop = asyncio.new_event_loop()

    def close(self) -> None:
        """Close the API and the loop."""
        with self._lock:
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
        """Run a coroutine on this session's loop; callers on other threads wait their turn."""
        with self._lock:
            if self._loop is None:
                coro.close()
                raise RuntimeError("LucidSession.start() not called")
            return self._loop.run_until_complete(coro)

    # -- auth ----------------------------------------------------------------
    def _make_api(self):
        if self._api_factory is not None:
            return self._api_factory()
        from lucidmotors import LucidAPI  # pylint: disable=import-outside-toplevel
        # Spelled out although it is the default: with it on, the library wakes the car
        # before every command on its own, and only command() decides that here.
        return LucidAPI(auto_wake=False)

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

    def command(self, lucid_vehicle: Any, action: Callable[[Any, Any], Any], wake_first: bool = False) -> None:
        """Run one vehicle command on the shared API and loop. Callable from any thread.

        The only path that wakes the car. With `wake_first`, the caller's reading of the last
        poll by mapping.is_awake, the car is woken explicitly, once, and given WAKE_SETTLE_S
        to listen. Not lucidmotors' vehicle_is_awake: that calls only SLEEP asleep, and a car
        in CLOUD_2 or SLEEP_CHARGE does not take a command either. The command itself is sent
        once: nothing here retries it, because a retried unlock or trunk is a second
        unlock or trunk if the first one landed and only its answer was lost."""
        async def _go():
            # One deadline for the lot: a login or refresh, the wake, the settle pause and the command.
            try:
                async with asyncio.timeout(self._command_timeout_s):
                    api = await self._ensure()  # raises CarConnectivity errors already
                    try:
                        if wake_first:
                            LOG.info("vehicle is asleep, waking it for a command")
                            await api.wakeup_vehicle(lucid_vehicle)
                            await asyncio.sleep(self._wake_settle_s)
                        await action(api, lucid_vehicle)
                    except Exception as exc:  # pylint: disable=broad-except
                        raise translate_api_error(exc) from exc
            except TimeoutError as exc:
                raise RetrievalError(f"no answer from Lucid within {self._command_timeout_s:.0f}s; check the car before trying again") from exc
        self.run(_go())
