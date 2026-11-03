"""The charging schedule: read off the car's own request (not the poll), published as plain
attributes, and written by the charging-schedule command in the format the car itself uses."""

import json
import time
from datetime import timedelta

import pytest

from carconnectivity.carconnectivity import CarConnectivity
from carconnectivity.errors import CommandError, SetterError
from lucidmotors.gen import vehicle_state_service_pb2 as pb

from carconnectivity_connectors.lucid import connector as connector_module
from carconnectivity_connectors.lucid import mapping
from carconnectivity_connectors.lucid.auth.session import LucidSession
from carconnectivity_connectors.lucid.connector import Connector
from carconnectivity_connectors.lucid.vehicle import parse_schedule

VIN = "LUCIDTESTVIN00042"


# -- the pure parts ---------------------------------------------------------------
@pytest.mark.parametrize("text,expected", [("23:00", "23:00"), ("6:05", "06:05"), ("23:00:00", "23:00"), ("2300", "23:00"), ("24:00", None),
                                           ("12:75", None), ("", None), (None, None), ("noon", None)])
def test_a_schedule_time_in_whatever_form_the_car_sends(text, expected):
    assert mapping.schedule_clock(text) == expected


@pytest.mark.parametrize("sample,written", [(None, "23:05"), ("", "23:05"), ("06:00", "23:05"), ("6:00", "23:05"), ("06:00:00", "23:05:00"),
                                            ("0600", "2305"), ("6am", None), ("2026-01-01T06:00:00Z", None)])
def test_a_write_copies_the_format_the_car_wrote(sample, written):
    assert mapping.schedule_clock_like(sample, 23 * 60 + 5) == written


def test_no_schedule_message_is_all_unknown():
    assert set(mapping.charging_schedule(None).values()) == {None}


def test_a_real_schedule_message():
    car = pb.ChargingSchedule(schedule_on=True, start_time="23:00:00", end_time="06:00:00", weekday_on=False, charging_available=True)
    read = mapping.charging_schedule(car)
    assert read == {"on": True, "start": "23:00", "end": "06:00", "weekdays": False, "available": True, "raw_start": "23:00:00", "raw_end": "06:00:00"}


@pytest.mark.parametrize("value,expected", [
    ("off", {"on": False, "start": None, "end": None, "weekdays": None}),
    ("ON", {"on": True, "start": None, "end": None, "weekdays": None}),
    ("23:00-06:30", {"on": True, "start": 1380, "end": 390, "weekdays": None}),
    ({"command": "off"}, {"on": False, "start": None, "end": None, "weekdays": None}),
    ({"on": True, "start": "1:00", "end": "5:30", "weekdays": True}, {"on": True, "start": 60, "end": 330, "weekdays": True}),
    ({"on": False}, {"on": False, "start": None, "end": None, "weekdays": None}),
])
def test_what_the_command_accepts(value, expected):
    assert parse_schedule(value) == expected


@pytest.mark.parametrize("value", ["", "tomorrow", "25:00-06:00", "23:00-", "23:00-06:61", 5, None, {"on": "yes"}, {"on": True, "start": "23:00"},
                                   {"on": True, "stop": "06:00"}, {"start": "23:00", "end": "06:00"}])
def test_what_it_refuses_before_the_car_sees_it(value):
    with pytest.raises(SetterError):
        parse_schedule(value)


def test_a_request_is_wanted_over_held():
    held = mapping.charging_schedule(pb.ChargingSchedule(schedule_on=False, start_time="23:00", end_time="06:00", weekday_on=True))
    assert mapping.schedule_request(held, parse_schedule("on")) == (True, "23:00", "06:00", True)
    assert mapping.schedule_request(held, parse_schedule("00:30-05:00")) == (True, "00:30", "05:00", True)
    assert mapping.schedule_request(held, parse_schedule({"on": True, "start": "01:00", "end": "02:00", "weekdays": False})) == (True, "01:00", "02:00", False)


def test_nothing_to_turn_on_and_a_format_not_known_are_refused():
    never = mapping.charging_schedule(pb.ChargingSchedule())
    with pytest.raises(ValueError, match="holds no schedule times"):
        mapping.schedule_request(never, parse_schedule("on"))
    assert mapping.schedule_request(never, parse_schedule("23:00-06:00")) == (True, "23:00", "06:00", False)
    odd = mapping.charging_schedule(pb.ChargingSchedule(start_time="11pm", end_time="6am"))
    with pytest.raises(ValueError, match="does not know"):
        mapping.schedule_request(odd, parse_schedule("23:00-06:00"))


# -- through a connector ----------------------------------------------------------
class FakeAPI:
    def __init__(self):
        self.schedule = pb.ChargingSchedule(schedule_on=True, start_time="23:00", end_time="06:00", weekday_on=False, charging_available=True)
        self.power = pb.POWER_STATE_ACCESSORY
        self.polls = 0
        self.reads = 0
        self.calls = []
        self.fail_read = None

    async def __aenter__(self):
        return self

    async def login_with_refresh_token(self, token):
        pass

    @property
    def session_time_remaining(self):
        return timedelta(seconds=300)

    async def fetch_vehicles(self):
        self.polls += 1
        state = pb.VehicleState(last_updated_ms=1_700_000_000_000 + self.polls * 60_000, power=self.power)
        return [pb.Vehicle(vehicle_id="vid-1", config=pb.VehicleConfig(vin=VIN, nickname="Bravo", model=pb.MODEL_GRAVITY), state=state)]

    async def get_charging_schedule(self, vehicle):
        self.reads += 1
        if self.fail_read is not None:
            raise self.fail_read
        return self.schedule

    async def set_charging_schedule(self, vehicle, schedule_on, start_time, end_time, weekday_on=False):
        self.calls.append((vehicle.vehicle_id, schedule_on, start_time, end_time, weekday_on))
        self.schedule = pb.ChargingSchedule(schedule_on=schedule_on, start_time=start_time, end_time=end_time, weekday_on=weekday_on, charging_available=True)

    async def close(self):
        pass


@pytest.fixture(autouse=True)
def _no_ntp(monkeypatch):
    monkeypatch.setattr("carconnectivity.carconnectivity.ntp_time_delta", lambda: None)


def _rig(tmp_path, commands):
    token = tmp_path / "token.json"
    token.write_text(json.dumps({"refresh_token": "opaque-test-token"}))
    cc = CarConnectivity(config={"carConnectivity": {}}, tokenstore_file=str(tmp_path / "tokens"), cache_file=str(tmp_path / "cache"))
    connector = Connector("lucid", cc, {"refresh_token_file": str(token), "commands": commands})
    api = FakeAPI()
    connector._session = LucidSession(token, api_factory=lambda: api)
    connector._session._wake_settle_s = 0
    connector._session.start()
    return connector, api, cc


@pytest.fixture
def rig(tmp_path):
    connector, api, cc = _rig(tmp_path, True)
    try:
        yield connector, api, cc
    finally:
        connector._session.close()
        cc.shutdown()


@pytest.fixture
def readonly(tmp_path):
    connector, api, cc = _rig(tmp_path, False)
    try:
        yield connector, api, cc
    finally:
        connector._session.close()
        cc.shutdown()


def test_the_schedule_is_published_and_read_once_an_hour_not_per_poll(rig):
    connector, api, cc = rig
    for _ in range(3):
        connector.fetch_vehicles()
    vehicle = cc.garage.get_vehicle(VIN)
    assert api.reads == 1, "three polls, one read"
    assert (vehicle.charging_schedule_on.value, vehicle.charging_schedule_start.value, vehicle.charging_schedule_end.value) == (True, "23:00", "06:00")
    assert vehicle.charging_schedule_weekdays.value is False and vehicle.charging_schedule_available.value is True


def test_it_is_read_again_after_the_hour(rig, monkeypatch):
    connector, api, cc = rig
    connector.fetch_vehicles()
    api.schedule = pb.ChargingSchedule(schedule_on=False, start_time="22:00", end_time="05:00")
    now = time.monotonic()
    monkeypatch.setattr(connector_module.time, "monotonic", lambda: now + connector_module.SCHEDULE_READ_S + 1)
    connector.fetch_vehicles()
    assert api.reads == 2
    assert cc.garage.get_vehicle(VIN).charging_schedule_start.value == "22:00"


def test_a_failed_read_neither_breaks_the_poll_nor_repeats_on_the_next_one(rig):
    connector, api, cc = rig
    api.fail_read = RuntimeError("boom")
    connector.fetch_vehicles()
    connector.fetch_vehicles()
    vehicle = cc.garage.get_vehicle(VIN)
    assert api.reads == 1 and vehicle.charging_schedule_on.value is None and vehicle.charging_schedule_start.value is None
    assert vehicle.state.value is not None, "the poll still went through"


def test_the_command_writes_in_the_cars_format_and_reads_it_back(rig):
    connector, api, cc = rig
    connector.fetch_vehicles()
    vehicle = cc.garage.get_vehicle(VIN)
    vehicle.charging.commands.commands["charging-schedule"].value = "00:30-05:15"
    assert api.calls == [("vid-1", True, "00:30", "05:15", False)]
    connector.fetch_vehicles()
    assert api.reads == 2, "read back at once after a write"
    assert (vehicle.charging_schedule_start.value, vehicle.charging_schedule_end.value) == ("00:30", "05:15")


def test_off_keeps_the_times(rig):
    connector, api, cc = rig
    connector.fetch_vehicles()
    cc.garage.get_vehicle(VIN).charging.commands.commands["charging-schedule"].value = "off"
    assert api.calls == [("vid-1", False, "23:00", "06:00", False)]


def test_a_bad_time_never_reaches_the_car(rig):
    connector, api, cc = rig
    connector.fetch_vehicles()
    with pytest.raises(SetterError):
        cc.garage.get_vehicle(VIN).charging.commands.commands["charging-schedule"].value = "25:00-06:00"
    assert api.calls == []


def test_unread_schedule_is_refused_not_guessed(rig):
    connector, api, cc = rig
    api.fail_read = RuntimeError("boom")
    connector.fetch_vehicles()
    with pytest.raises(CommandError, match="has not been read"):
        cc.garage.get_vehicle(VIN).charging.commands.commands["charging-schedule"].value = "off"
    assert api.calls == []


def test_a_car_that_writes_times_in_a_form_we_do_not_know_is_refused(rig):
    connector, api, cc = rig
    api.schedule = pb.ChargingSchedule(schedule_on=True, start_time="11pm", end_time="6am")
    connector.fetch_vehicles()
    with pytest.raises(CommandError, match="does not know"):
        cc.garage.get_vehicle(VIN).charging.commands.commands["charging-schedule"].value = "23:00-06:00"
    assert api.calls == []


def test_a_sleeping_car_is_woken_first_like_every_command(rig):
    connector, api, cc = rig
    api.power = pb.POWER_STATE_SLEEP
    api.wakes = 0

    async def wake(vehicle):
        api.wakes += 1
    api.wakeup_vehicle = wake
    connector.fetch_vehicles()
    cc.garage.get_vehicle(VIN).charging.commands.commands["charging-schedule"].value = "on"
    assert api.wakes == 1 and len(api.calls) == 1


def test_read_only_has_the_attributes_but_no_command(readonly):
    connector, api, cc = readonly
    connector.fetch_vehicles()
    vehicle = cc.garage.get_vehicle(VIN)
    assert vehicle.charging_schedule_start.value == "23:00"
    assert not vehicle.charging.commands.contains_command("charging-schedule")
    assert api.calls == []


def test_a_library_without_the_calls_leaves_the_schedule_unknown(rig, monkeypatch):
    connector, api, cc = rig
    monkeypatch.setattr(Connector, "_library_has", staticmethod(lambda call: False))
    connector.fetch_vehicles()
    vehicle = cc.garage.get_vehicle(VIN)
    assert api.reads == 0 and vehicle.charging_schedule_on.value is None
    with pytest.raises(CommandError, match="upgrade"):
        vehicle.charging.commands.commands["charging-schedule"].value = "off"
