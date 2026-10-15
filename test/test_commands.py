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
from carconnectivity_connectors.lucid import mapping
from carconnectivity_connectors.lucid.connector import Connector

VIN = "LUCIDTESTVIN00042"


class FakeAPI:
    """Records every call as (method, args). The car's power state is `power`, read as the
    next poll's; asleep or awake is the connector's call from that, as on a real car."""

    def __init__(self):
        self.power = 3  # ACCESSORY: awake
        self.charge_state = None
        self.config = {}  # VehicleConfig fields beyond identity: the capability flags
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
        # lucidmotors 1.4.2's own answer, false only for POWER_STATE_SLEEP; the connector
        # must not rely on it, since CLOUD_2 and the rest are asleep too.
        return vehicle.state.power != 1

    async def fetch_vehicles(self):
        self.calls.append(("fetch_vehicles", ()))
        await self._busy()
        state = SimpleNamespace(last_updated_ms=1_700_000_000_000, power=self.power, charging=SimpleNamespace(charge_state=self.charge_state))
        return [SimpleNamespace(vehicle_id="vid-1", config=SimpleNamespace(vin=VIN, nickname="Alpha", model=2, **self.config), state=state)]

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

    def __getattr__(self, name):
        # The 0.3.0 commands, recorded the same way. Only names the real library has: a
        # test against a call that does not exist would pass here and fail on the car,
        # so test_every_faked_call_is_one_the_library_has checks this list against it.
        if name not in LIBRARY_CALLS:
            raise AttributeError(name)

        async def call(v, *args, **kwargs):
            await self._record(name, v.vehicle_id, *args, *([kwargs] if kwargs else []))
        return call


LIBRARY_CALLS = {
    "sentry_mode_on": (),
    "sentry_mode_off": (),
    "enhanced_deterrence_on": (),
    "enhanced_deterrence_off": (),
    "sentry_mode_at_home_on": (),
    "sentry_mode_at_home_off": (),
    "sentry_mode_at_work_on": (),
    "sentry_mode_at_work_off": (),
    "turn_off_sentry_alarm": (),
    "alarm_control": ("mode",),
    "open_all_windows": (),
    "close_all_windows": (),
    "lights_on": (),
    "lights_off": (),
    "max_ac_on": (),
    "max_ac_off": (),
    "creature_comfort_control": ("mode",),
    "steering_wheel_heater_control": ("level",),
    "seat_climate_control": ("kwargs",),
    "battery_precon_on": (),
    "battery_precon_off": (),
    "set_ac_current_limit": ("current_limit",),
}


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


def _poll_sees(rig, power):
    """The car goes to `power` and the next poll sees it, as a command would find it."""
    rig.api.power = power
    rig.connector.fetch_vehicles()
    rig.api.calls.clear()


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
    rig.api.power = 1
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


@pytest.mark.parametrize("unit,temperature,celsius", [("°F", 68.0, 20.0), ("°C", 21.5, 21.5)])
def test_climatization_takes_the_unit_as_a_string(rig, unit, temperature, celsius):
    """Over MQTT or REST the unit arrives as JSON, so a string, not the enum."""
    cmd = rig.vehicle.climatization.commands.commands["start-stop"]
    cmd.value = {"command": "start", "target_temperature": temperature, "target_temperature_unit": unit}
    assert rig.api.calls == [("set_cabin_temperature", ("vid-1", celsius))]


@pytest.mark.parametrize("unit", ["F", "fahrenheit", 5, None])
def test_climatization_refuses_a_unit_it_does_not_know(rig, unit):
    cmd = rig.vehicle.climatization.commands.commands["start-stop"]
    with pytest.raises(CommandError):
        cmd.value = {"command": "start", "target_temperature": 21.0, "target_temperature_unit": unit}
    assert rig.api.calls == []


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


def test_a_flash_that_fails_after_the_horn_says_the_horn_sounded(rig, monkeypatch):
    """So nobody retries honk-and-flash blindly and honks a second time."""
    async def lights_flash(v):
        rig.api.calls.append(("lights_flash", (v.vehicle_id,)))
        raise type("E", (Exception,), {"code": SimpleNamespace(name="UNAVAILABLE")})("down")
    monkeypatch.setattr(rig.api, "lights_flash", lights_flash)
    with pytest.raises(CommandError, match="horn sounded but the lights did not flash"):
        rig.vehicle.commands.commands["honk-flash"].value = "honk-and-flash"
    assert rig.api.names() == ["honk_horn", "lights_flash"]


def test_wake_wakes_once_and_sleep_is_refused(rig):
    _poll_sees(rig, 1)
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
    _poll_sees(rig, 1)
    cmd.value = "lock"
    assert rig.api.names() == ["wakeup_vehicle", "doors_lock"]


@pytest.mark.parametrize("power", [6, 8, 9, 10, 12, 13])
def test_every_power_state_the_poller_calls_asleep_is_woken_for_a_command(rig, power):
    """The library calls only SLEEP asleep; CLOUD_2 and the rest take no command either."""
    _poll_sees(rig, power)
    rig.vehicle.doors.commands.commands["lock-unlock"].value = "lock"
    assert rig.api.names() == ["wakeup_vehicle", "doors_lock"]


def test_a_car_charging_in_sleep_charge_is_awake_and_not_woken(rig):
    rig.api.charge_state = 8  # CHARGING
    _poll_sees(rig, 6)
    rig.vehicle.doors.commands.commands["lock-unlock"].value = "lock"
    assert rig.api.names() == ["doors_lock"]


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


def test_every_faked_call_is_one_the_library_has():
    """The fake answers to these names; the real LucidAPI must too, with the same arguments."""
    import inspect  # pylint: disable=import-outside-toplevel

    lucidmotors = pytest.importorskip("lucidmotors")
    for name, params in LIBRARY_CALLS.items():
        method = getattr(lucidmotors.LucidAPI, name)
        assert tuple(inspect.signature(method).parameters)[1:] == ("vehicle", *params), name
    # seat_climate_control takes its zones as keyword arguments; each one sent must be a field of the request.
    fields = set(lucidmotors.gen.vehicle_state_service_pb2.SeatClimateControlRequest.DESCRIPTOR.fields_by_name)
    sent = {zone for zones in mapping.SEAT_ZONES.values() for zone in zones}
    assert sent and sent <= fields


# -- 0.3.0: Halo Secure ----------------------------------------------------------
@pytest.mark.parametrize(
    "name,value,method",
    [
        ("sentry", "on", "sentry_mode_on"),
        ("sentry", "off", "sentry_mode_off"),
        ("enhanced-deterrence", "on", "enhanced_deterrence_on"),
        ("enhanced-deterrence", "off", "enhanced_deterrence_off"),
        ("sentry-at-home", "on", "sentry_mode_at_home_on"),
        ("sentry-at-home", "off", "sentry_mode_at_home_off"),
        ("sentry-at-work", "on", "sentry_mode_at_work_on"),
        ("sentry-at-work", "off", "sentry_mode_at_work_off"),
        ("sentry-alarm", "off", "turn_off_sentry_alarm"),
    ],
)
def test_halo_secure(rig, name, value, method):
    rig.vehicle.commands.commands[name].value = value
    assert rig.api.calls == [(method, ("vid-1",))]


def test_a_sentry_alarm_can_only_be_silenced(rig):
    with pytest.raises(CommandError, match="off"):
        rig.vehicle.commands.commands["sentry-alarm"].value = "on"
    assert rig.api.calls == []


@pytest.mark.parametrize("value,mode", [("off", 1), ("on", 2), ("silent", 3)])
def test_alarm_mode(rig, value, mode):
    rig.vehicle.commands.commands["alarm"].value = value
    assert rig.api.calls == [("alarm_control", ("vid-1", mode))]


def test_the_new_commands_wake_a_sleeping_car_first(rig):
    _poll_sees(rig, 1)
    rig.vehicle.commands.commands["sentry"].value = "off"
    assert rig.api.names() == ["wakeup_vehicle", "sentry_mode_off"]


def test_a_failed_new_command_is_sent_once_and_named(rig):
    rig.api.fail_with = type("E", (Exception,), {"code": SimpleNamespace(name="UNAVAILABLE")})("down")
    with pytest.raises(CommandError, match="^turn off sentry failed:"):
        rig.vehicle.commands.commands["sentry"].value = "off"
    assert rig.api.names() == ["sentry_mode_off"]


# -- 0.3.0: windows, headlights ---------------------------------------
@pytest.mark.parametrize(
    "path,value,method",
    [
        (("commands", "windows"), "vent", "open_all_windows"),
        (("commands", "windows"), "close", "close_all_windows"),
        (("commands", "lights"), "on", "lights_on"),
        (("commands", "lights"), "off", "lights_off"),
    ],
)
def test_windows_and_headlights(rig, path, value, method):
    owner = rig.vehicle if path[0] == "commands" else getattr(rig.vehicle, path[0])
    owner.commands.commands[path[1]].value = value
    assert rig.api.calls == [(method, ("vid-1",))]


def test_windows_do_not_claim_a_full_open(rig):
    with pytest.raises(SetterError):
        rig.vehicle.commands.commands["windows"].value = "open"
    assert rig.api.calls == []


# -- 0.3.0: climate ------------------------------------------------------------------
@pytest.mark.parametrize(
    "name,value,call",
    [
        ("max-ac", "on", ("max_ac_on", ("vid-1",))),
        ("max-ac", "off", ("max_ac_off", ("vid-1",))),
        ("creature-comfort", "on", ("creature_comfort_control", ("vid-1", 2))),
        ("creature-comfort", "off", ("creature_comfort_control", ("vid-1", 1))),
        ("steering-wheel-heater", "off", ("steering_wheel_heater_control", ("vid-1", 1))),
        ("steering-wheel-heater", "low", ("steering_wheel_heater_control", ("vid-1", 2))),
        ("steering-wheel-heater", "high", ("steering_wheel_heater_control", ("vid-1", 4))),
    ],
)
def test_max_ac_creature_comfort_and_steering_wheel(rig, name, value, call):
    rig.vehicle.climatization.commands.commands[name].value = value
    assert rig.api.calls == [call]


@pytest.mark.parametrize(
    "value,zones",
    [
        (
            "driver heat high",
            {"driver_heat_backrest_zone1": 5, "driver_heat_backrest_zone3": 5, "driver_heat_cushion_zone2": 5, "driver_heat_cushion_zone4": 5},
        ),
        ("front-passenger vent low", {"front_passenger_vent_backrest": 3, "front_passenger_vent_cushion": 3}),
        ({"seat": "rear-center", "mode": "heat", "level": "off"}, {"rear_passenger_heat_center": 2}),
        ("rear-right heat medium", {"rear_passenger_heat_right": 4}),
    ],
)
def test_seat_climate_sets_every_zone_of_one_seat(rig, value, zones):
    rig.vehicle.climatization.commands.commands["seat-climate"].value = value
    assert rig.api.calls == [("seat_climate_control", ("vid-1", zones))]


@pytest.mark.parametrize("name,value", [("seat-climate", "rear-left vent high"), ("seat-climate", "driver heat 9"), ("steering-wheel-heater", "4")])
def test_climate_levels_out_of_range_never_reach_the_car(rig, name, value):
    with pytest.raises(SetterError):
        rig.vehicle.climatization.commands.commands[name].value = value
    assert rig.api.calls == []


# -- 0.4.0: commands the car's config says it is not fitted for are refused before sending --
# Availability enums: 0 unknown, 1 unavailable, 2 available. RearSeatConfig 2 is six seats.
@pytest.mark.parametrize(
    "config,name,value",
    [
        ({"front_seats_ventilation": 1}, "seat-climate", "driver vent low"),
        ({"front_seats_heating": 1}, "seat-climate", "front-passenger heat high"),
        ({"second_row_heated_seats": 1}, "seat-climate", "rear-left heat low"),
        ({"rear_seat_config": 2}, "seat-climate", "rear-center heat low"),
        ({"heated_steering_wheel": 1}, "steering-wheel-heater", "high"),
    ],
)
def test_a_control_the_car_says_it_lacks_is_refused_before_sending(rig, config, name, value):
    rig.api.config = config
    _poll_sees(rig, 3)
    with pytest.raises(CommandError, match="Not sent: this car"):
        rig.vehicle.climatization.commands.commands[name].value = value
    assert rig.api.calls == []


@pytest.mark.parametrize(
    "config,name,value",
    [
        ({"front_seats_ventilation": 0, "front_seats_heating": 2}, "seat-climate", "driver vent low"),  # unknown is not unavailable
        ({"front_seats_ventilation": 1}, "seat-climate", "driver heat low"),  # no vent is not no heat
        ({"rear_seat_config": 2}, "seat-climate", "rear-right heat low"),  # six seats still have a right one
        ({"rear_seat_config": 3}, "seat-climate", "rear-center heat low"),
        ({"heated_steering_wheel": 0}, "steering-wheel-heater", "high"),
    ],
)
def test_a_car_that_has_it_or_has_not_said_keeps_the_control(rig, config, name, value):
    rig.api.config = config
    _poll_sees(rig, 3)
    rig.vehicle.climatization.commands.commands[name].value = value
    assert len(rig.api.calls) == 1


def test_a_gas_strut_frunk_opens_but_is_not_closed_remotely(rig):
    rig.api.config = {"frunk_strut": 1}
    _poll_sees(rig, 3)
    with pytest.raises(CommandError, match="gas strut"):
        rig.vehicle.doors.commands.commands["frunk"].value = "close"
    rig.vehicle.doors.commands.commands["frunk"].value = "open"
    assert rig.api.names() == ["frunk_open"]
    rig.api.config = {"frunk_strut": 2}
    _poll_sees(rig, 3)
    rig.vehicle.doors.commands.commands["frunk"].value = "close"
    assert rig.api.names() == ["frunk_close"]


# -- 0.3.0: charging -----------------------------------------------------------------
@pytest.mark.parametrize("value,method", [("on", "battery_precon_on"), ("off", "battery_precon_off")])
def test_battery_preconditioning(rig, value, method):
    rig.vehicle.charging.commands.commands["battery-preconditioning"].value = value
    assert rig.api.calls == [(method, ("vid-1",))]


def test_ac_current_limit_is_not_settable_by_default(readonly):
    current = readonly.vehicle.charging.settings.maximum_current
    assert not current._is_changeable
    with pytest.raises(TypeError):
        current.value = 32
    assert readonly.api.calls == []


def test_ac_current_limit_sets_through_the_library(rig):
    current = rig.vehicle.charging.settings.maximum_current
    assert current._is_changeable and (current.minimum, current.maximum, current.precision) == (8.0, 80.0, 1.0)
    current.value = 32
    assert rig.api.calls == [("set_ac_current_limit", ("vid-1", 32))]


@pytest.mark.parametrize("bad", [7, 81, 0, -16, 16.5, "abc", True, float("nan"), None])
def test_ac_current_limit_rejects_bad_values(rig, bad):
    assert rig.vehicle.charging.settings.maximum_current._is_changeable, "refused by the connector, not by a read-only attribute"
    with pytest.raises((SetterError, ValueError, TypeError)):
        rig.vehicle.charging.settings.maximum_current.value = bad
    assert rig.api.calls == []


def test_no_charge_port_command(rig):
    """An Air's charge port door is opened by hand; the API's call for a powered one is not offered."""
    assert "charge-port" not in rig.vehicle.doors.commands.commands
