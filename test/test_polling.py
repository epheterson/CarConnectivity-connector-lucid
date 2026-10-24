"""Gentler polling: a sleeping car is read slowly, an awake one as before, a command or a
`fast_poll` request brings the awake interval back at once, and the token is refreshed for the
wait that follows. The simulated day counts requests on a fake clock against a fake API."""
import json
from datetime import timedelta

import pytest

from carconnectivity.carconnectivity import CarConnectivity
from carconnectivity.errors import SetterError
from carconnectivity_connectors.lucid import mapping
from carconnectivity_connectors.lucid.auth.session import LucidSession, REFRESH_MARGIN_S, REFRESH_SLACK_S
from carconnectivity_connectors.lucid.connector import Connector, FAST_POLL_MAX_S
from carconnectivity.vehicle import GenericVehicle

ASLEEP, PARKED, DRIVING = GenericVehicle.State.OFFLINE, GenericVehicle.State.PARKED, GenericVehicle.State.DRIVING
HOUR = 3600


@pytest.fixture(autouse=True)
def _no_ntp(monkeypatch):
    monkeypatch.setattr("carconnectivity.carconnectivity.ntp_time_delta", lambda: None)


# -- the interval policy --------------------------------------------------------------

def test_a_sleeping_car_waits_the_sleep_interval_and_nothing_else_does():
    assert mapping.poll_interval([ASLEEP], 60, 15, 240) == 240
    assert mapping.poll_interval([PARKED], 60, 15, 240) == 60
    assert mapping.poll_interval([DRIVING], 60, 15, 240) == 15
    assert mapping.poll_interval([None], 60, 15, 240) == 60, "a car that has not said is not assumed asleep"
    assert mapping.poll_interval([], 60, 15, 240) == 60, "no cars is not a reason to slow"


def test_one_awake_or_driving_car_keeps_the_whole_account_fast():
    assert mapping.poll_interval([ASLEEP, PARKED], 60, 15, 240) == 60
    assert mapping.poll_interval([ASLEEP, DRIVING], 60, 15, 240) == 15


def test_without_a_sleep_interval_nothing_changes():
    assert mapping.poll_interval([ASLEEP], 60, 15) == 60


# -- config ----------------------------------------------------------------------------

def _connector(tmp_path, config=None):
    token = tmp_path / "token.json"
    token.write_text(json.dumps({"refresh_token": "opaque-test-token"}))
    cc = CarConnectivity(config={"carConnectivity": {}}, tokenstore_file=str(tmp_path / "tokens"), cache_file=str(tmp_path / "cache"))
    return Connector("lucid", cc, {"refresh_token_file": str(token), **(config or {})}), cc


def test_config_defaults_and_bounds(tmp_path):
    connector, cc = _connector(tmp_path)
    assert connector.active_config['sleep_interval'] == 60, "unchanged unless asked"
    cc.shutdown()
    connector, cc = _connector(tmp_path, {"interval": 300})
    assert connector.active_config['sleep_interval'] == 300
    cc.shutdown()
    connector, cc = _connector(tmp_path, {"sleep_interval": 180})
    assert connector.active_config['sleep_interval'] == 180
    cc.shutdown()
    for bad in ({"sleep_interval": 59}, {"sleep_interval": 901}):
        with pytest.raises(ValueError):
            _connector(tmp_path, bad)


# -- the connector's wait ----------------------------------------------------------------

def _sleeping(connector, monkeypatch, states):
    monkeypatch.setattr(connector, "_vehicle_states", lambda: states)


def test_the_wait_follows_the_car_and_the_token_is_planned_for_it(tmp_path, monkeypatch):
    connector, cc = _connector(tmp_path, {"sleep_interval": 240})
    _sleeping(connector, monkeypatch, [ASLEEP])
    assert connector._next_interval(60) == 240
    assert connector._session._refresh_margin_s == 240 + REFRESH_SLACK_S
    _sleeping(connector, monkeypatch, [PARKED])
    assert connector._next_interval(60) == 60
    assert connector._session._refresh_margin_s == 60 + REFRESH_SLACK_S
    assert connector._session._refresh_margin_s >= REFRESH_MARGIN_S
    cc.shutdown()


def test_fast_poll_holds_the_awake_interval_and_asks_for_a_read_now(tmp_path, monkeypatch):
    connector, cc = _connector(tmp_path, {"sleep_interval": 240})
    _sleeping(connector, monkeypatch, [ASLEEP])
    connector.fast_poll.value = timedelta(seconds=300)
    assert connector._poll_now.is_set(), "the read the caller is waiting for"
    assert connector._next_interval(60) == 60, "a sleeper is read at the awake interval while asked"
    connector.fast_poll.value = timedelta(seconds=0)
    assert connector._next_interval(60) == 240, "0 cancels"
    cc.shutdown()


def test_fast_poll_expires(tmp_path, monkeypatch):
    connector, cc = _connector(tmp_path, {"sleep_interval": 240})
    _sleeping(connector, monkeypatch, [ASLEEP])
    connector.fast_poll.value = timedelta(seconds=30)
    now = [1000.0]
    monkeypatch.setattr("carconnectivity_connectors.lucid.connector.time.monotonic", lambda: now[0])
    connector._awake_until = 1000.0 + 30
    assert connector._next_interval(60) == 60
    now[0] += 31
    assert connector._next_interval(60) == 240, "bounded: it does not outlast what was asked"
    cc.shutdown()


@pytest.mark.parametrize("bad", [timedelta(seconds=FAST_POLL_MAX_S + 1), timedelta(seconds=-1), "soon", True])
def test_fast_poll_refuses_what_is_out_of_bounds(tmp_path, bad):
    connector, cc = _connector(tmp_path)
    with pytest.raises(SetterError):
        connector._on_fast_poll(connector.fast_poll, bad)
    assert connector._awake_until == 0.0
    cc.shutdown()


def test_a_command_brings_the_awake_interval_back_at_once(tmp_path, monkeypatch):
    connector, cc = _connector(tmp_path, {"commands": True, "sleep_interval": 240})
    _sleeping(connector, monkeypatch, [ASLEEP])
    connector._lucid_vehicles["VIN"] = object()
    vehicle = type("V", (), {"vin": type("A", (), {"value": "VIN"})()})()
    connector._session.command = lambda *a, **k: None
    connector._send(vehicle, "test", lambda api, v: None)
    assert connector._next_interval(60) == 60, "the wake may not show on the early poll; the next one still must not wait 4 minutes"
    cc.shutdown()


# -- the simulated day ----------------------------------------------------------------

class FakeClockAPI:
    """Counts requests; a token lasts 300 s on the fake clock and refreshes at the real rule."""

    def __init__(self, clock):
        self.clock = clock
        self.fetches = self.refreshes = 0
        self.expires = clock[0] + 300

    async def login_with_refresh_token(self, token):
        self.expires = self.clock[0] + 300

    @property
    def session_time_remaining(self):
        return timedelta(seconds=self.expires - self.clock[0])

    async def authentication_refresh(self):
        self.refreshes += 1
        self.expires = self.clock[0] + 300

    async def fetch_vehicles(self):
        assert self.expires > self.clock[0], "a poll went out on a dead token"
        self.fetches += 1
        return ["car"]

    async def close(self):
        pass


# (start, end, state) in seconds into the day; asleep everywhere else.
DAY = [
    (7 * HOUR + 50 * 60, 8 * HOUR, PARKED), (8 * HOUR, 8 * HOUR + 30 * 60, DRIVING), (8 * HOUR + 30 * 60, 9 * HOUR, PARKED),
    (12 * HOUR, 12 * HOUR + 10 * 60, PARKED),
    (17 * HOUR + 30 * 60, 17 * HOUR + 40 * 60, PARKED), (17 * HOUR + 40 * 60, 18 * HOUR + 10 * 60, DRIVING), (18 * HOUR + 10 * 60, 18 * HOUR + 40 * 60, PARKED),
    (21 * HOUR, 23 * HOUR, PARKED),  # charging: awake
]


def _state_at(t, day):
    return next((s for a, b, s in day if a <= t < b), ASLEEP)


def _simulate(tmp_path, sleep_s, day=None, plan=True):
    """One car through DAY, polled as the connector polls; returns requests and the worst
    time between the car waking and a poll seeing it."""
    day = DAY if day is None else day
    clock = [0.0]
    api = FakeClockAPI(clock)
    session = LucidSession(tmp_path / "token.json", api_factory=lambda: api)
    (tmp_path / "token.json").write_text(json.dumps({"refresh_token": "opaque-684-char-token"}))
    session.start()
    worst_lag, woke_at = 0.0, None
    while clock[0] < 24 * HOUR:
        session.fetch_vehicles()
        seen = _state_at(clock[0], day)
        truly_awake = seen != ASLEEP
        # when did the current awake stretch begin?
        if truly_awake and woke_at is None:
            woke_at = next(a for a, b, _ in day if a <= clock[0] < b)
            while any(b == woke_at for _, b, _ in day):  # joined stretches (parked then driving) count from the first
                woke_at = next(a for a, b, _ in day if b == woke_at)
            worst_lag = max(worst_lag, clock[0] - woke_at)
        if not truly_awake:
            woke_at = None
        wait = mapping.poll_interval([seen], 60, 15, sleep_s)
        if plan:
            session.plan_next_poll(wait)
        clock[0] += wait
    session.close()
    return api.fetches + api.refreshes, api.fetches, api.refreshes, worst_lag


def test_a_simulated_day_default_config_costs_what_it_did_and_opting_in_costs_less(tmp_path, capsys):
    """Before: 60 s awake or asleep, 15 s driving, the refresh margin a flat 60 s. Default
    config now: the same waits, the margin the wait plus 30 s. Opted in: a sleeper every 240 s."""
    for sub in "abc":
        (tmp_path / sub).mkdir()
    before = _simulate(tmp_path / "a", None, plan=False)
    default = _simulate(tmp_path / "b", 60)
    opted = _simulate(tmp_path / "c", 240)
    with capsys.disabled():
        for name, r in (("before", before), ("default", default), ("sleep_interval 240", opted)):
            print(f"\n{name}: {r[0]} requests/day ({r[1]} reads + {r[2]} refreshes); worst wake-to-read lag {r[3]:.0f}s")
    assert abs(default[0] - before[0]) <= before[0] * 0.05, "the default must not change what the connector costs"
    assert default[3] == 0, "a wake is read on the first poll after it"
    assert opted[0] < before[0] * 0.65
    assert opted[3] <= 240
