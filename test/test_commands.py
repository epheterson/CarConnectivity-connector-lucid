"""Commands, against a fake LucidAPI: off unless the config turns them on, the right
library call with the right arguments, bad values refused before the car hears of them,
nothing retried, and only a command ever wakes the car."""

import asyncio
import json
import sys
import threading
from datetime import timedelta
from types import SimpleNamespace

import pytest

from carconnectivity.carconnectivity import CarConnectivity
from carconnectivity.errors import CommandError, SetterError
from carconnectivity.units import Temperature
from carconnectivity_connectors.lucid.auth.session import LucidSession
from carconnectivity_connectors.lucid.connector import Connector

VIN = "LUCIDTESTVIN00042"


class FakeAPI:
    """Records every call as (method, args). Awake or asleep is whatever `asleep` says."""

    def __init__(self):
        self.asleep = False
        self.calls = []
        self.fail_with = None
        self.hang = False
        self.in_flight = 0
        self.max_in_flight = 0

    async def __aenter__(self):
        return self

    async def login_with_refresh_token(self, token):
        self.calls.append(("login", ()))

    @property
    def session_time_remaining(self):
        return timedelta(seconds=300)

    def vehicle_is_awake(self, vehicle):
        return not self.asleep

    async def fetch_vehicles(self):
        self.calls.append(("fetch_vehicles", ()))
        await self._busy()
        return [
            SimpleNamespace(
                vehicle_id="vid-1", config=SimpleNamespace(vin=VIN, nickname="Alpha", model=2), state=SimpleNamespace(last_updated_ms=1_700_000_000_000)
            )
        ]

    async def close(self):
        pass

    async def _busy(self):
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        await asyncio.sleep(0.02)
        self.in_flight -= 1

    async def _record(self, name, *args):
        self.calls.append((name, args))
        await self._busy()
        if self.hang:
            await asyncio.sleep(10)
        if self.fail_with is not None:
            raise self.fail_with

    async def wakeup_vehicle(self, v):
        await self._record("wakeup_vehicle", v.vehicle_id)

    async def start_charging(self, v):
        await self._record("start_charging", v.vehicle_id)

    async def stop_charging(self, v):
        await self._record("stop_charging", v.vehicle_id)

    async def set_charge_limit(self, v, value):
        await self._record("set_charge_limit", v.vehicle_id, value)

    async def set_cabin_temperature(self, v, temp):
        await self._record("set_cabin_temperature", v.vehicle_id, temp)

    async def defrost_on(self, v):
        await self._record("defrost_on", v.vehicle_id)

    async def defrost_off(self, v):
        await self._record("defrost_off", v.vehicle_id)

    async def doors_lock(self, v):
        await self._record("doors_lock", v.vehicle_id)

    async def doors_unlock(self, v):
        await self._record("doors_unlock", v.vehicle_id)

    async def trunk_open(self, v):
        await self._record("trunk_open", v.vehicle_id)

    async def trunk_close(self, v):
        await self._record("trunk_close", v.vehicle_id)

    async def frunk_open(self, v):
        await self._record("frunk_open", v.vehicle_id)

    async def frunk_close(self, v):
        await self._record("frunk_close", v.vehicle_id)

    async def lights_flash(self, v):
        await self._record("lights_flash", v.vehicle_id)

    async def honk_horn(self, v):
        await self._record("honk_horn", v.vehicle_id)

    def names(self):
        return [name for name, _ in self.calls]


@pytest.fixture(autouse=True)
def _no_ntp(monkeypatch):
    """CarConnectivity asks an NTP server for the time on construction; a test has no
    business on the network, and a slow answer cost five seconds a test."""
    monkeypatch.setattr("carconnectivity.carconnectivity.ntp_time_delta", lambda: None)


def _rig(tmp_path, config):
    token = tmp_path / "token.json"
    token.write_text(json.dumps({"refresh_token": "opaque-test-token"}))
    cc = CarConnectivity(config={"carConnectivity": {}}, tokenstore_file=str(tmp_path / "tokens"), cache_file=str(tmp_path / "cache"))
    connector = Connector("lucid", cc, {"refresh_token_file": str(token), **config})
    api = FakeAPI()
    connector._session = LucidSession(token, api_factory=lambda: api)
    connector._session._wake_settle_s = 0
    connector._session.start()
    connector.fetch_vehicles()
    api.calls.clear()
    return SimpleNamespace(connector=connector, api=api, vehicle=cc.garage.get_vehicle(VIN), cc=cc)


@pytest.fixture
def rig(tmp_path):
    """A connector with commands enabled, a started session on a fake API, one vehicle fetched."""
    r = _rig(tmp_path, {"commands": True})
    try:
        yield r
    finally:
        r.connector._session.close()
        r.cc.shutdown()


@pytest.fixture
def readonly(tmp_path):
    """The same, with the config saying nothing about commands."""
    r = _rig(tmp_path, {})
    try:
        yield r
    finally:
        r.connector._session.close()
        r.cc.shutdown()


# -- off by default -------------------------------------------------------------
def _all_commands(vehicle):
    return [
        c
        for cmds in (vehicle.commands, vehicle.charging.commands, vehicle.climatization.commands, vehicle.window_heatings.commands, vehicle.doors.commands)
        for c in cmds.commands.values()
    ]


def test_no_command_exists_unless_the_config_asks(readonly):
    assert readonly.connector.active_config["commands"] is False
    assert _all_commands(readonly.vehicle) == []
    readonly.connector.fetch_vehicles()
    assert _all_commands(readonly.vehicle) == []


def test_charge_limit_is_not_settable_by_default(readonly):
    target = readonly.vehicle.charging.settings.target_level
    assert not target._is_changeable
    with pytest.raises(TypeError):
        target.value = 80
    assert readonly.api.calls == []


def test_send_refuses_when_disabled_even_if_reached(readonly):
    """No command object exists to reach it, but if something did, nothing goes out."""
    with pytest.raises(CommandError):
        readonly.connector._send(readonly.vehicle, "unlock doors", lambda api, v: api.doors_unlock(v))
    assert readonly.api.calls == []


@pytest.mark.parametrize("bad", ["true", "false", 1, 0, None, "yes"])
def test_commands_must_be_a_real_boolean(tmp_path, bad):
    token = tmp_path / "token.json"
    token.write_text(json.dumps({"refresh_token": "opaque-test-token"}))
    cc = CarConnectivity(config={"carConnectivity": {}}, tokenstore_file=str(tmp_path / "tokens"), cache_file=str(tmp_path / "cache"))
    try:
        with pytest.raises(ValueError):
            Connector("lucid", cc, {"refresh_token_file": str(token), "commands": bad})
    finally:
        cc.shutdown()


def test_the_real_library_is_built_without_auto_wake(tmp_path, monkeypatch):
    seen = {}

    class Spy:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setitem(sys.modules, "lucidmotors", SimpleNamespace(LucidAPI=Spy))
    LucidSession(tmp_path / "token.json")._make_api()
    assert seen == {"auto_wake": False}


# -- enabled -----------------------------------------------------------------------
def test_commands_appear_at_their_paths(rig):
    v = rig.vehicle
    assert v.charging.commands.contains_command("start-stop")
    assert v.climatization.commands.contains_command("start-stop")
    assert v.window_heatings.commands.contains_command("start-stop")
    for name in ("lock-unlock", "trunk", "frunk"):
        assert v.doors.commands.contains_command(name)
    assert v.commands.contains_command("honk-flash")
    assert v.commands.contains_command("wake-sleep")


def test_another_poll_does_not_duplicate_or_rehook(rig):
    rig.connector.fetch_vehicles()
    rig.api.calls.clear()
    rig.vehicle.charging.commands.commands["start-stop"].value = "start"
    assert rig.api.names().count("start_charging") == 1


def test_polling_never_wakes_a_sleeping_car(rig):
    rig.api.asleep = True
    for _ in range(3):
        rig.connector.fetch_vehicles()
    assert set(rig.api.names()) == {"fetch_vehicles"}


@pytest.mark.parametrize("value,method", [("start", "start_charging"), ("stop", "stop_charging")])
def test_charging_start_stop(rig, value, method):
    rig.vehicle.charging.commands.commands["start-stop"].value = value
    assert rig.api.calls == [(method, ("vid-1",))]


def test_an_unknown_value_never_reaches_the_car(rig):
    with pytest.raises(SetterError):
        rig.vehicle.charging.commands.commands["start-stop"].value = "explode"
    with pytest.raises(SetterError):
        rig.vehicle.doors.commands.commands["trunk"].value = "explode"
    assert rig.api.calls == []


def test_charge_limit_sets_through_the_library(rig):
    target = rig.vehicle.charging.settings.target_level
    assert target._is_changeable and (target.minimum, target.maximum, target.precision) == (50.0, 100.0, 1.0)
    target.value = 80
    assert rig.api.calls == [("set_charge_limit", ("vid-1", 80))]
    assert target.value == 80


@pytest.mark.parametrize("bad", [49, 101, 80.5, "abc", -1, 0, float("nan")])
def test_charge_limit_rejects_bad_values(rig, bad):
    with pytest.raises((SetterError, ValueError)):
        rig.vehicle.charging.settings.target_level.value = bad
    assert rig.api.calls == []


def test_climatization_start_uses_celsius_target_and_stop_turns_it_off(rig):
    cmd = rig.vehicle.climatization.commands.commands["start-stop"]
    cmd.value = "start --target-temperature 21.5"
    cmd.value = "stop"
    assert rig.api.calls == [("set_cabin_temperature", ("vid-1", 21.5)), ("set_cabin_temperature", ("vid-1", None))]


def test_climatization_converts_fahrenheit(rig):
    cmd = rig.vehicle.climatization.commands.commands["start-stop"]
    cmd.value = {"command": "start", "target_temperature": 68.0, "target_temperature_unit": Temperature.F}
    assert rig.api.calls == [("set_cabin_temperature", ("vid-1", 20.0))]


def test_climatization_rejects_an_absurd_temperature(rig):
    cmd = rig.vehicle.climatization.commands.commands["start-stop"]
    with pytest.raises(CommandError):
        cmd.value = "start --target-temperature 90"
    assert rig.api.calls == []


@pytest.mark.parametrize("value,method", [("start", "defrost_on"), ("stop", "defrost_off")])
def test_defrost(rig, value, method):
    rig.vehicle.window_heatings.commands.commands["start-stop"].value = value
    assert rig.api.calls == [(method, ("vid-1",))]


@pytest.mark.parametrize("value,method", [("lock", "doors_lock"), ("unlock", "doors_unlock")])
def test_lock_unlock(rig, value, method):
    rig.vehicle.doors.commands.commands["lock-unlock"].value = value
    assert rig.api.calls == [(method, ("vid-1",))]


@pytest.mark.parametrize(
    "name,value,method",
    [("trunk", "open", "trunk_open"), ("trunk", "close", "trunk_close"), ("frunk", {"command": "open"}, "frunk_open"), ("frunk", "close", "frunk_close")],
)
def test_trunk_and_frunk(rig, name, value, method):
    rig.vehicle.doors.commands.commands[name].value = value
    assert rig.api.calls == [(method, ("vid-1",))]


def test_flash_and_honk_and_flash(rig):
    cmd = rig.vehicle.commands.commands["honk-flash"]
    cmd.value = "flash"
    assert rig.api.names() == ["lights_flash"]
    rig.api.calls.clear()
    cmd.value = "honk-and-flash"
    assert rig.api.names() == ["honk_horn", "lights_flash"]


def test_wake_wakes_once_and_sleep_is_refused(rig):
    rig.api.asleep = True
    cmd = rig.vehicle.commands.commands["wake-sleep"]
    cmd.value = "wake"
    assert rig.api.calls == [("wakeup_vehicle", ("vid-1",))]
    with pytest.raises(CommandError):
        cmd.value = "sleep"
    assert rig.api.names() == ["wakeup_vehicle"]


def test_a_command_wakes_a_sleeping_car_first_and_not_an_awake_one(rig):
    cmd = rig.vehicle.doors.commands.commands["lock-unlock"]
    cmd.value = "lock"
    assert rig.api.names() == ["doors_lock"]
    rig.api.calls.clear()
    rig.api.asleep = True
    cmd.value = "lock"
    assert rig.api.names() == ["wakeup_vehicle", "doors_lock"]


@pytest.mark.parametrize(
    "path,value,method",
    [(("doors", "lock-unlock"), "unlock", "doors_unlock"), (("doors", "trunk"), "open", "trunk_open"), (("doors", "frunk"), "open", "frunk_open")],
)
def test_a_failed_unlock_or_opening_is_sent_once_and_never_retried(rig, path, value, method):
    rig.api.fail_with = type("E", (Exception,), {"code": SimpleNamespace(name="UNAVAILABLE")})("down")
    with pytest.raises(CommandError):
        getattr(rig.vehicle, path[0]).commands.commands[path[1]].value = value
    assert rig.api.names() == [method]


@pytest.mark.parametrize("code", ["RESOURCE_EXHAUSTED", "UNAUTHENTICATED", "INTERNAL", "FAILED_PRECONDITION"])
def test_every_library_error_becomes_a_short_command_error(rig, code):
    rig.api.fail_with = type("E", (Exception,), {"code": SimpleNamespace(name=code)})("nope")
    with pytest.raises(CommandError) as info:
        rig.vehicle.doors.commands.commands["lock-unlock"].value = "lock"
    assert str(info.value).startswith("lock doors failed:")
    assert "opaque-test-token" not in str(info.value)


def test_a_command_lucid_never_answers_times_out_once(rig):
    rig.connector._session._command_timeout_s = 0.1
    rig.api.hang = True
    with pytest.raises(CommandError, match="no answer"):
        rig.vehicle.doors.commands.commands["lock-unlock"].value = "unlock"
    assert rig.api.names() == ["doors_unlock"]


def test_a_failed_command_logs_no_vin_or_token(rig, caplog):
    rig.api.fail_with = type("E", (Exception,), {"code": SimpleNamespace(name="UNAVAILABLE")})("down")
    with caplog.at_level("DEBUG"):
        with pytest.raises(CommandError):
            rig.vehicle.doors.commands.commands["lock-unlock"].value = "lock"
    assert VIN not in caplog.text and VIN[-4:] in caplog.text
    assert "opaque-test-token" not in caplog.text


def test_a_successful_command_asks_for_a_refresh(rig, caplog, monkeypatch):
    monkeypatch.setattr("carconnectivity_connectors.lucid.connector.REFRESH_AFTER_COMMAND_S", 0.01)
    with caplog.at_level("INFO", logger="carconnectivity.connectors.lucid"):
        rig.vehicle.doors.commands.commands["lock-unlock"].value = "lock"
    assert rig.connector._poll_now.wait(2)
    assert "lock doors" in caplog.text and VIN not in caplog.text


def test_commands_from_other_threads_never_overlap_the_poller(rig):
    errors = []

    def command():
        try:
            rig.vehicle.doors.commands.commands["lock-unlock"].value = "lock"
        except Exception as err:  # pylint: disable=broad-except
            errors.append(err)

    threads = [threading.Thread(target=command) for _ in range(4)]
    for t in threads:
        t.start()
    for _ in range(4):
        rig.connector.fetch_vehicles()
    for t in threads:
        t.join()
    assert not errors
    assert rig.api.max_in_flight == 1
    assert rig.api.names().count("login") == 0, "no second login"
    assert rig.api.names().count("doors_lock") == 4
