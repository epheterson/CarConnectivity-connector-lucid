"""The 0.5.0 reads, against real protobuf messages: each one set, unset (proto3's zero, which is the
car not saying) and, where the Gravity reading of 2026-10-14 settled a case, as that car sent it."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from carconnectivity.carconnectivity import CarConnectivity
from lucidmotors.gen import charging_service_pb2 as cpb
from lucidmotors.gen import sentry_service_pb2 as spb
from lucidmotors.gen import vehicle_state_service_pb2 as pb

from carconnectivity_connectors.lucid import connector as connector_module
from carconnectivity_connectors.lucid import mapping as m
from carconnectivity_connectors.lucid.auth.session import LucidSession
from carconnectivity_connectors.lucid.connector import Connector
from carconnectivity_connectors.lucid.vehicle import ChargeStatus, DriveMode, Gear, LucidElectricVehicle, MpbFault, ScheduledCharge, UpdateStatus

MEASURED = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
VIN = "LUCIDTESTVIN00042"


@pytest.fixture
def vehicle(tmp_path):
    cc = CarConnectivity(config={"carConnectivity": {}}, tokenstore_file=str(tmp_path / "tokens"), cache_file=str(tmp_path / "cache"))
    try:
        yield LucidElectricVehicle(vin="TESTVIN0000000001", garage=cc.garage)
    finally:
        cc.shutdown()


# ---- times ---------------------------------------------------------------------
@pytest.mark.parametrize("value,expected", [
    (1_780_000_000, datetime(2026, 5, 28, 20, 26, 40, tzinfo=timezone.utc)),
    (1_780_000_000_000, datetime(2026, 5, 28, 20, 26, 40, tzinfo=timezone.utc)),
    (0, None), (8932, None), (123_456_789, None), (9_999_999_999_999, None), (-5, None),
])
def test_an_epoch_in_seconds_or_milliseconds_is_told_by_its_size(value, expected):
    assert m.epoch_time(value) == expected


def test_a_reading_is_stamped_with_its_own_time_unless_that_is_not_credible():
    assert m.stamped(MEASURED - timedelta(hours=9), MEASURED) == MEASURED - timedelta(hours=9)
    assert m.stamped(None, MEASURED) == MEASURED
    assert m.stamped(MEASURED + timedelta(days=1), MEASURED) == MEASURED, "a time after the poll is the car's clock being wrong"


def test_a_stale_fix_is_dated_when_it_was_taken(vehicle):
    taken = int((MEASURED - timedelta(hours=9)).timestamp())
    gps = pb.Gps(location=pb.Location(latitude=39.78, longitude=-89.65), position_time=taken)
    Connector._apply_position(vehicle, pb.VehicleState(gps=gps, power=pb.POWER_STATE_SLEEP), MEASURED)
    assert vehicle.position.latitude.last_updated == MEASURED - timedelta(hours=9)


def test_a_fix_without_a_time_is_dated_by_the_poll(vehicle):
    gps = pb.Gps(location=pb.Location(latitude=39.78, longitude=-89.65), position_time=0)
    Connector._apply_position(vehicle, pb.VehicleState(gps=gps), MEASURED)
    assert vehicle.position.latitude.last_updated == MEASURED


# ---- charging -------------------------------------------------------------------
def charging(vehicle, **fields):
    Connector._apply_charging(vehicle, pb.VehicleState(charging=pb.ChargingState(**fields)), MEASURED)


def test_scheduled_charge_and_its_time(vehicle):
    charging(vehicle, charge_state=pb.CHARGE_STATE_CABLE_CONNECTED, scheduled_charge=pb.SCHEDULED_CHARGE_STATE_SCHEDULED_TO_CHARGE, charge_scheduled_time=1380)
    assert vehicle.scheduled_charge.value is ScheduledCharge.SCHEDULED and vehicle.scheduled_charge_time_raw.value == 1380


def test_an_idle_schedule_has_no_time(vehicle):
    charging(vehicle, scheduled_charge=pb.SCHEDULED_CHARGE_STATE_IDLE, charge_scheduled_time=1380)
    assert vehicle.scheduled_charge.value is ScheduledCharge.IDLE and vehicle.scheduled_charge_time_raw.value is None


def test_a_schedule_the_car_has_not_said_is_unknown(vehicle):
    charging(vehicle)
    assert vehicle.scheduled_charge.value is None


def test_session_figures_only_while_a_session_moves_energy(vehicle):
    charging(vehicle, charge_state=pb.CHARGE_STATE_CHARGING, charge_session_mi=142.0, port_power=11.2)
    assert vehicle.charge_session_range_raw.value == 142.0 and vehicle.charge_port_power_raw.value == 11.2
    charging(vehicle, charge_state=pb.CHARGE_STATE_NOT_CONNECTED, charge_session_mi=142.0, port_power=11.2)
    assert vehicle.charge_session_range_raw.value is None and vehicle.charge_port_power_raw.value is None


def test_discharge_energy_only_while_discharging(vehicle):
    charging(vehicle, charge_state=pb.CHARGE_STATE_DISCHARGING, discharge_energy=3.5)
    assert vehicle.discharge_energy_raw.value == 3.5
    charging(vehicle, charge_state=pb.CHARGE_STATE_CHARGING, discharge_energy=3.5)
    assert vehicle.discharge_energy_raw.value is None


def test_the_chargers_current_limit_needs_a_cable(vehicle):
    """A parked, unplugged Gravity sends 80, its own setting, not a charger's limit."""
    charging(vehicle, charge_state=pb.CHARGE_STATE_NOT_CONNECTED, active_session_ac_current_limit=80)
    assert vehicle.charge_session_current_limit.value is None
    charging(vehicle, charge_state=pb.CHARGE_STATE_CHARGING, active_session_ac_current_limit=32)
    assert vehicle.charge_session_current_limit.value == 32


@pytest.mark.parametrize("raw,expected", [(pb.GENERAL_CHARGE_DERATED_CHARGING_POWER, ChargeStatus.DERATED), (pb.GENERAL_CHARGE_DEFAULT, ChargeStatus.NORMAL),
                                          (pb.GENERAL_CHARGE_SAVETIME_TEMP_PRECON, ChargeStatus.PRECONDITIONING_SAVES_TIME), (pb.GENERAL_CHARGE_UNKNOWN, None)])
def test_why_a_charge_is_slow(vehicle, raw, expected):
    state = pb.VehicleState(charging=pb.ChargingState(), notifications=pb.Notifications(charging_general_status=raw))
    Connector._apply_charging(vehicle, state, MEASURED)
    assert vehicle.charge_status.value is expected


# ---- battery, software ------------------------------------------------------------
def test_range_the_car_holds_back(vehicle):
    Connector._apply_cabin(vehicle, pb.VehicleState(battery=pb.BatteryState(unavailable_range=18.4)), MEASURED)
    assert vehicle.battery_range_unavailable.value == pytest.approx(18.4)
    Connector._apply_cabin(vehicle, pb.VehicleState(battery=pb.BatteryState(unavailable_range=0.0)), MEASURED + timedelta(minutes=1))
    assert vehicle.battery_range_unavailable.value is None


@pytest.mark.parametrize("state,percent,shown,status", [
    (pb.UPDATE_STATE_IN_PROGRESS, 40, 40.0, UpdateStatus.INSTALLING),
    (pb.UPDATE_STATE_SUCCESS, 100, None, UpdateStatus.SUCCEEDED),
    (pb.UPDATE_STATE_FAILED, 12, None, UpdateStatus.FAILED),
    (pb.UPDATE_FAILED_NO_ACTION, 0, None, UpdateStatus.FAILED),
    (pb.UPDATE_FAILED_DRIVE_ALLOWED, 0, None, UpdateStatus.FAILED_DRIVABLE),
    (pb.UPDATE_STATE_WAITING_ON_BCM, 3, 3.0, UpdateStatus.WAITING),
    (pb.UPDATE_STATE_UNKNOWN, 0, None, None),
])
def test_an_install_in_progress_and_how_it_ended(vehicle, state, percent, shown, status):
    Connector._apply_cabin(vehicle, pb.VehicleState(software_update=pb.SoftwareUpdate(state=state, percent_complete=percent)), MEASURED)
    assert vehicle.software_update_state.value is status and vehicle.software_update_percent.value == shown


# ---- security, tires, drive ----------------------------------------------------------
def test_the_sentry_alarm_and_its_range_cost(vehicle):
    multiplex = pb.SentryMultiplexValues(sentry_location_settings_raw=3)
    sentry = pb.SentryState(remote_alarm_state=pb.SENTRY_REMOTE_ALARM_ON, range_cost_per_day=7, multiplex_values=multiplex)
    Connector._apply_security(vehicle, pb.VehicleState(sentry_state=sentry), MEASURED)
    assert vehicle.sentry_alarm_sounding.value is True and vehicle.sentry_range_cost_per_day_raw.value == 7
    assert vehicle.sentry_location_settings_raw.value == 3


def test_what_the_gravity_sent_with_sentry_idle_is_unknown_not_zero(vehicle):
    """range_cost_per_day 0 and no multiplex block, as read 2026-10-14."""
    sentry = pb.SentryState(enablement_state=pb.SENTRY_STATE_IDLE, range_cost_per_day=0)
    Connector._apply_security(vehicle, pb.VehicleState(sentry_state=sentry), MEASURED)
    assert vehicle.sentry_range_cost_per_day_raw.value is None and vehicle.sentry_location_settings_raw.value is None
    assert vehicle.sentry_alarm_sounding.value is None


def test_a_multiplex_block_of_zero_is_a_zero_setting(vehicle):
    sentry = pb.SentryState(multiplex_values=pb.SentryMultiplexValues(sentry_location_settings_raw=0))
    Connector._apply_security(vehicle, pb.VehicleState(sentry_state=sentry), MEASURED)
    assert vehicle.sentry_location_settings_raw.value == 0


def test_tires_defective_and_stale(vehicle):
    taken = int((MEASURED - timedelta(hours=14)).timestamp() * 1000)
    on, off = pb.TIRE_PRESSURE_SENSOR_DEFECTIVE_ON, pb.TIRE_PRESSURE_SENSOR_DEFECTIVE_OFF
    chassis = pb.ChassisState(front_left_tire_pressure_bar=2.5, tire_pressure_last_updated=taken,
                              sensor_defective_left_front=on, sensor_defective_left_rear=off)
    Connector._apply_tires(vehicle, pb.VehicleState(chassis=chassis), MEASURED)
    assert vehicle.tire_sensor_defective.value is True
    assert vehicle.tire_pressure_updated.value == MEASURED - timedelta(hours=14)
    assert vehicle.tire_pressure_front_left.last_updated == MEASURED - timedelta(hours=14)


def test_every_sensor_fine_and_none_said():
    ok, bad = pb.TIRE_PRESSURE_SENSOR_DEFECTIVE_OFF, pb.TIRE_PRESSURE_SENSOR_DEFECTIVE_ON
    assert m.tire_sensor_defective([ok, ok, 0, ok]) is False
    assert m.tire_sensor_defective([0, 0]) is None
    assert m.tire_sensor_defective([ok, bad]) is True


def test_gear_mode_privacy_and_fault(vehicle):
    state = pb.VehicleState(gear_position=pb.GEAR_PARK, drive_mode=pb.DRIVE_MODE_SPORT_PLUS, privacy_mode=pb.PRIVACY_MODE_CONNECTIVITY_DISABLED,
                            fault_state=pb.FaultState(mpb_fault_status=pb.MPB_FAULT_STATUS_CRITICAL_HAPS))
    Connector._apply_controls(vehicle, state, MEASURED)
    assert vehicle.gear.value is Gear.PARK and vehicle.drive_mode.value is DriveMode.SPORT_PLUS and vehicle.valet.value is False
    assert vehicle.privacy_mode.value is True and vehicle.mpb_fault.value is MpbFault.CRITICAL


@pytest.mark.parametrize("mode", [pb.DRIVE_MODE_UNKNOWN, pb.DRIVE_MODE_RESERVED_1, pb.DRIVE_MODE_FACTORY, pb.DRIVE_MODE_DEV1])
def test_modes_that_are_not_ones_an_owner_is_in_read_unknown(vehicle, mode):
    Connector._apply_controls(vehicle, pb.VehicleState(drive_mode=mode), MEASURED)
    assert vehicle.drive_mode.value is None


def test_privacy_on_a_car_that_has_not_said_is_unknown(vehicle):
    Connector._apply_controls(vehicle, pb.VehicleState(), MEASURED)
    assert vehicle.privacy_mode.value is None and vehicle.gear.value is None and vehicle.mpb_fault.value is None


# ---- identity ----------------------------------------------------------------------
def test_trim_paint_and_the_ea_plan(vehicle):
    expires = int(datetime(2028, 5, 1, tzinfo=timezone.utc).timestamp())
    cfg = pb.VehicleConfig(vin="TESTVIN0000000001", model=pb.MODEL_GRAVITY, variant=pb.MODEL_VARIANT_GRAND_TOURING, paint_color=pb.PAINT_COLOR_ZENITH_RED,
                           ea_subscription=pb.ChargingSubscription(name="Plan A", expiration_date=expires))
    Connector._apply_identity(vehicle, cfg, pb.VehicleState(), MEASURED)
    assert vehicle.trim.value == "Grand Touring" and vehicle.paint_color.value == "Zenith Red"
    assert vehicle.ea_plan_name.value == "Plan A" and vehicle.ea_plan_expires.value == datetime(2028, 5, 1, tzinfo=timezone.utc)


def test_a_config_that_says_none_of_it_is_unknown(vehicle):
    Connector._apply_identity(vehicle, pb.VehicleConfig(vin="TESTVIN0000000001"), pb.VehicleState(), MEASURED)
    assert vehicle.trim.value is None and vehicle.paint_color.value is None and vehicle.ea_plan_name.value is None and vehicle.ea_plan_expires.value is None


# ---- Sentry events and charge records ---------------------------------------------------
def event(event_id, seconds, level=2, kind=spb.EVENT_TYPE_BREAKIN, lat=39.78, lon=-89.65, media=2):
    e = spb.Event(event_id=event_id, event_threat_level=level, event_type=kind, vehicle_id="vid-1", primary_user_id="user-secret",
                  event_geo_location=spb.EventGeoLocation(latitude=lat, longitude=lon))
    e.recorded_at.seconds = seconds
    for _ in range(media):
        e.media_list.add(media_download_url="https://cloud.invalid/secret-picture")
    return e


def test_sentry_events_oldest_first_pictures_counted_not_linked():
    out = m.sentry_events([event("b", 1_780_000_100), event("a", 1_780_000_000, level=1, kind=spb.EVENT_TYPE_COLLISION, media=0)])
    rows = json.loads(out)
    assert [r["id"] for r in rows] == ["a", "b"]
    assert rows[1] == {"id": "b", "at": "2026-05-28T20:28:20Z", "level": 2, "type": "break-in", "lat": 39.78, "lon": -89.65, "pictures": 2}
    assert rows[0]["type"] == "collision" and rows[0]["pictures"] == 0
    assert "cloud.invalid" not in out and "user-secret" not in out and "vid-1" not in out


def test_an_event_without_a_place_or_a_time():
    rows = json.loads(m.sentry_events([event("a", 1_780_000_000, lat=0.0, lon=0.0)]))
    assert rows[0]["lat"] is None and rows[0]["lon"] is None
    assert m.sentry_events([event("", 1_780_000_000), event("x", 0)]) is None
    assert m.sentry_events([]) is None and m.sentry_events(None) is None


def record(record_id, start, **fields):
    r = cpb.ChargingDataRecord(id=record_id, auth_id="card-secret", currency="USD", **fields)
    r.start_datetime.seconds = start
    r.stop_datetime.seconds = start + 3600
    r.location.name = "Plaza Charging"
    r.location.address = "1 Secret Street"
    return r


def test_charge_records_keep_the_numbers_as_sent_and_nothing_personal():
    out = m.charging_records([record("r2", 1_780_003_600, total_energy=30.0), record("r1", 1_780_000_000, total_energy=12.5, total_time=3600.0)])
    rows = json.loads(out)
    assert [r["id"] for r in rows] == ["r1", "r2"]
    assert rows[0] == {"id": "r1", "start": "2026-05-28T20:26:40Z", "stop": "2026-05-28T21:26:40Z", "energy": 12.5, "total_time": 3600.0,
                       "charge_time": None, "idle_time": None, "parking_time": None, "charger": "Plaza Charging", "currency": "USD"}
    assert "Secret" not in out and "card-secret" not in out
    assert m.charging_records([]) is None


# ---- the slow reads, through a connector ----------------------------------------------------
class FakeAPI:
    def __init__(self):
        self.polls = 0
        self.events = [event("a", 1_780_000_000)]
        self.records = [record("r1", 1_780_000_000, total_energy=12.5)]
        self.asked = []
        self.fail = False
        self.ema_id = "EMA-1"

    async def __aenter__(self):
        return self

    async def login_with_refresh_token(self, token):
        pass

    @property
    def session_time_remaining(self):
        return timedelta(seconds=300)

    async def fetch_vehicles(self):
        self.polls += 1
        state = pb.VehicleState(last_updated_ms=1_700_000_000_000 + self.polls * 60_000, power=pb.POWER_STATE_SLEEP)
        return [pb.Vehicle(vehicle_id="vid-1", config=pb.VehicleConfig(vin=VIN, nickname="Bravo", ema_id=self.ema_id), state=state)]

    async def get_sentry_events(self, vehicle_id, start_time_utc, end_time_utc, offset=0, limit=100):
        self.asked.append(("events", vehicle_id, end_time_utc - start_time_utc, limit))
        if self.fail:
            raise RuntimeError("boom")
        return spb.GetEventsResponse(events=self.events)

    async def get_charging_data_records(self, ema_id, offset=0, limit=50):
        self.asked.append(("records", ema_id, limit))
        return self.records

    async def get_charging_schedule(self, vehicle):
        return pb.ChargingSchedule()

    async def close(self):
        pass


@pytest.fixture(autouse=True)
def _no_ntp(monkeypatch):
    monkeypatch.setattr("carconnectivity.carconnectivity.ntp_time_delta", lambda: None)


@pytest.fixture
def rig(tmp_path):
    token = tmp_path / "token.json"
    token.write_text(json.dumps({"refresh_token": "opaque-test-token"}))
    cc = CarConnectivity(config={"carConnectivity": {}}, tokenstore_file=str(tmp_path / "tokens"), cache_file=str(tmp_path / "cache"))
    connector = Connector("lucid", cc, {"refresh_token_file": str(token)})
    api = FakeAPI()
    connector._session = LucidSession(token, api_factory=lambda: api)
    connector._session.start()
    try:
        yield connector, api, cc
    finally:
        connector._session.close()
        cc.shutdown()


def test_events_and_records_are_read_once_not_per_poll(rig):
    connector, api, cc = rig
    for _ in range(3):
        connector.fetch_vehicles()
    vehicle = cc.garage.get_vehicle(VIN)
    assert api.asked == [("events", "vid-1", connector_module.SENTRY_EVENTS_WINDOW, 100), ("records", "EMA-1", 50)]
    assert json.loads(vehicle.sentry_events.value)[0]["id"] == "a" and json.loads(vehicle.charging_records.value)[0]["id"] == "r1"


def test_no_account_id_no_records(rig):
    connector, api, cc = rig
    api.ema_id = ""
    connector.fetch_vehicles()
    assert [a[0] for a in api.asked] == ["events"] and cc.garage.get_vehicle(VIN).charging_records.value is None


def test_a_failed_events_read_keeps_what_was_published_and_waits(rig, monkeypatch):
    connector, api, cc = rig
    connector.fetch_vehicles()
    api.fail = True
    now = connector_module.time.monotonic()
    monkeypatch.setattr(connector_module.time, "monotonic", lambda: now + connector_module.SENTRY_EVENTS_READ_S + 1)
    connector.fetch_vehicles()
    connector.fetch_vehicles()
    assert [a[0] for a in api.asked].count("events") == 2, "one failed retry, not one per poll"
    assert json.loads(cc.garage.get_vehicle(VIN).sentry_events.value)[0]["id"] == "a"
