"""Refresh cadence, error translation and the command deadline, with a fake LucidAPI."""
import asyncio
import json
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest

from carconnectivity.errors import AuthenticationError, RetrievalError, TemporaryAuthenticationError, TooManyRequestsError
from carconnectivity_connectors.lucid.auth.session import LucidSession, REFRESH_MARGIN_S, translate_api_error


class FakeAPI:
    def __init__(self, remaining_s):
        self.remaining_s = remaining_s
        self.logins = 0
        self.refreshes = 0
        self.fetches = 0

    async def __aenter__(self):
        return self

    async def login_with_refresh_token(self, token):
        assert token == "opaque-684-char-token"
        self.logins += 1

    @property
    def session_time_remaining(self):
        return timedelta(seconds=self.remaining_s)

    async def authentication_refresh(self):
        self.refreshes += 1
        self.remaining_s = 300

    async def fetch_vehicles(self):
        self.fetches += 1
        return ["TARS"]

    async def close(self):
        pass


@pytest.fixture
def token_file(tmp_path):
    p = tmp_path / "lucid-token.json"
    p.write_text(json.dumps({"refresh_token": "opaque-684-char-token"}))
    return p


def test_logs_in_once_and_does_not_refresh_while_fresh(token_file):
    fake = FakeAPI(remaining_s=299)
    s = LucidSession(token_file, api_factory=lambda: fake)
    s.start()
    for _ in range(10):  # ten polls inside the window
        assert s.fetch_vehicles() == ["TARS"]
    assert fake.logins == 1
    assert fake.refreshes == 0, "the old bridge refreshed on every poll; this must not"
    assert fake.fetches == 10
    s.close()


def test_refreshes_only_under_the_margin(token_file):
    fake = FakeAPI(remaining_s=REFRESH_MARGIN_S - 1)
    s = LucidSession(token_file, api_factory=lambda: fake)
    s.start()
    s.fetch_vehicles()
    assert fake.refreshes == 1
    s.fetch_vehicles()  # now 300 s remain
    assert fake.refreshes == 1
    s.close()


def test_missing_token_is_a_configuration_problem(tmp_path):
    s = LucidSession(tmp_path / "nope.json", api_factory=lambda: FakeAPI(300))
    s.start()
    with pytest.raises(AuthenticationError):
        s.api()
    s.close()


def test_error_translation():
    def err(code_name):
        return SimpleNamespace(code=SimpleNamespace(name=code_name))
    assert isinstance(translate_api_error(err("RESOURCE_EXHAUSTED")), TooManyRequestsError)
    assert isinstance(translate_api_error(err("UNAUTHENTICATED")), TemporaryAuthenticationError)


def _sent(calls):
    async def action(api, vehicle):
        calls.append("action")
    return action


def test_a_login_that_never_answers_is_inside_the_command_deadline(token_file):
    """_ensure() logs in or refreshes before the command; a hang there must not hold the
    session lock, and the poller with it, for good."""
    fake = FakeAPI(remaining_s=300)

    async def hang(token):
        await asyncio.sleep(10)
    fake.login_with_refresh_token = hang
    s = LucidSession(token_file, api_factory=lambda: fake)
    s._command_timeout_s = 0.1
    s.start()
    calls = []
    started = time.monotonic()
    with pytest.raises(RetrievalError, match="no answer"):
        s.command(SimpleNamespace(vehicle_id="vid-1"), _sent(calls))
    assert time.monotonic() - started < 2
    assert calls == []
    s.close()


def test_one_deadline_covers_wake_settle_and_command(token_file):
    """Not one per step: a wake, the settle pause and the command together get the deadline once."""
    fake = FakeAPI(remaining_s=300)

    async def wakeup_vehicle(vehicle):
        await asyncio.sleep(0.15)
    fake.wakeup_vehicle = wakeup_vehicle
    s = LucidSession(token_file, api_factory=lambda: fake)
    s._command_timeout_s = 0.25
    s._wake_settle_s = 0.15
    s.start()
    calls = []
    with pytest.raises(RetrievalError, match="no answer"):
        s.command(SimpleNamespace(vehicle_id="vid-1"), _sent(calls), wake_first=True)
    assert calls == []
    s.close()
