"""The 0.4.0 reads, against real protobuf messages rather than SimpleNamespace, and
against one real reading of a parked Gravity (gravity_reading).

A real message answers every field: one the car left out reads as 0, an empty string or
an empty sub-message, never None. These tests build VehicleState the way the library
hands it over, so each field is checked set, unset (proto3's zero) and unknown, and a
missing value has to come out as None or unknown, not as a confident wrong answer.
Beyond that reading, the enum names come from the proto.
"""

from datetime import datetime, timedelta, timezone

import pytest

from carconnectivity.carconnectivity import CarConnectivity
from lucidmotors.gen import vehicle_state_service_pb2 as pb

from carconnectivity_connectors.lucid import mapping as m
from carconnectivity_connectors.lucid.connector import Connector
from carconnectivity_connectors.lucid.vehicle import LucidElectricVehicle

MEASURED = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


def gravity_reading():
    """One read-only reading of a real Gravity (7-seat, power frunk strut), parked and
    unplugged, 2026-10-14, as the values came back; identity and place made up."""
    return pb.Vehicle(
        vehicle_id="vid-1",
        config=pb.VehicleConfig(vin="TESTVIN0000000001", model=pb.MODEL_GRAVITY, rear_seat_config=pb.REAR_SEAT_CONFIG_7_SEAT,
                                frunk_strut=pb.STRUT_TYPE_POWER, front_seats_heating=pb.FRONT_SEATS_HEATING_AVAILABLE,
                                front_seats_ventilation=pb.FRONT_SEATS_VENTILATION_AVAILABLE,
                                second_row_heated_seats=pb.SECOND_ROW_HEATED_SEATS_AVAILABLE,
                                heated_steering_wheel=pb.HEATED_STEERING_WHEEL_AVAILABLE),
        state=pb.VehicleState(
            power=pb.POWER_STATE_SLEEP,
            battery=pb.BatteryState(battery_health_level=100.0),
            chassis=pb.ChassisState(odometer_km=2936.0),
            charging=pb.ChargingState(charge_state=pb.CHARGE_STATE_NOT_CONNECTED, charge_session_kwh=0.0, charge_session_mi=0.0,
                                      session_minutes_remaining=65535, active_session_ac_current_limit=80),
            gps=pb.Gps(location=pb.Location(latitude=39.7817, longitude=-89.6501), elevation=8932, position_time=0),
            alarm=pb.AlarmState(mode=pb.ALARM_MODE_ON, status=pb.ALARM_STATUS_ARMED),
            sentry_state=pb.SentryState(enablement_state=pb.SENTRY_STATE_IDLE, sentry_active_state=pb.SentryActiveState(is_active=False),
                                        usb_drive_status=pb.SENTRY_USB_DRIVE_CONNECTED, range_cost_per_day=0),
            hvac=pb.HvacState(defrost=pb.DEFROST_OFF, rear_window_heating_status=pb.REAR_WINDOW_HEATING_STATUS_OFF),
            body=pb.BodyState(keyfob_battery_status=pb.KEYFOB_BATTERY_STATUS_UNKNOWN,
                              living_object_detection_status=pb.LIVING_OBJECT_DETECTION_STATUS_DISABLED,
                              unattended_occupant_detection_status=pb.UNATTENDED_OCCUPANT_DETECTION_STATUS_NO_REQUEST),
        ),
    )


def later(minutes):
    """A later poll. CarConnectivity drops a value measured no later than the one it holds."""
    return MEASURED + timedelta(minutes=minutes)


@pytest.fixture
def vehicle(tmp_path):
    cc = CarConnectivity(config={"carConnectivity": {}}, tokenstore_file=str(tmp_path / "tokens"), cache_file=str(tmp_path / "cache"))
    try:
        yield LucidElectricVehicle(vin="TESTVIN0000000001", garage=cc.garage)
    finally:
        cc.shutdown()


# ---- software update --------------------------------------------------------


def test_an_update_the_car_announces_is_published():
    st = pb.VehicleState(software_update=pb.SoftwareUpdate(update_available=pb.UPDATE_AVAILABLE))
    assert m.update_available(st.software_update.update_available) is True


@pytest.mark.parametrize("state", [pb.VehicleState(), pb.VehicleState(software_update=pb.SoftwareUpdate())])
def test_an_update_flag_the_car_left_out_is_not_no_update(vehicle, state):
    """UpdateAvailability has no "none waiting" value, and proto3 reads an absent field
    as 0. bool(0) published that as False: "no update", for a car that had not said."""
    Connector._apply_cabin(vehicle, state, MEASURED)
    assert vehicle.software_update_available.value is None


# ---- the charging session -----------------------------------------------------

CHARGING, CABLE_CONNECTED, NOT_CONNECTED = pb.CHARGE_STATE_CHARGING, pb.CHARGE_STATE_CABLE_CONNECTED, pb.CHARGE_STATE_NOT_CONNECTED


def apply_charging(vehicle, at=MEASURED, **charging):
    Connector._apply_charging(vehicle, pb.VehicleState(charging=pb.ChargingState(**charging)), at)


def test_the_cars_session_meter_is_published_while_charging(vehicle):
    apply_charging(vehicle, charge_state=CHARGING, charge_session_kwh=41.37, charge_session_mi=142.0, session_minutes_remaining=95)
    assert vehicle.charge_session_energy.value == pytest.approx(41.37)
    assert not hasattr(vehicle, "charge_session_range"), "charge_session_mi's unit is unverified; not published"
    assert vehicle.charging.estimated_date_reached.value == datetime(2026, 10, 14, 13, 35, tzinfo=timezone.utc), "from when the car measured it"


def test_a_parked_gravitys_zero_meter_is_not_a_session(vehicle):
    """The real reading: unplugged, 0.0 kWh and 0.0 mi. Not "a session of nothing"."""
    Connector._apply_charging(vehicle, gravity_reading().state, MEASURED)
    assert vehicle.charge_session_energy.value is None


@pytest.mark.parametrize("fields", [{}, {"charge_session_kwh": 0.0, "charge_session_mi": 0.0}])
def test_charging_with_an_unset_or_zero_meter_is_unknown_not_zero(vehicle, fields):
    """Proto3 sends 0.0 for a field left out; published as a reading, every session costs nothing."""
    apply_charging(vehicle, charge_state=CHARGING, **fields)
    assert vehicle.charge_session_energy.value is None


def test_plugged_in_but_not_charging_publishes_no_session(vehicle):
    apply_charging(vehicle, charge_state=CABLE_CONNECTED, charge_session_kwh=3.2, charge_session_mi=11.0)
    assert vehicle.charge_session_energy.value is None
    assert vehicle.charging.estimated_date_reached.value is None, "plugged in but not charging has no finish time"


@pytest.mark.parametrize("charge_state", [NOT_CONNECTED, pb.CHARGE_STATE_UNKNOWN])
def test_no_cable_or_no_word_publishes_no_session(vehicle, charge_state):
    apply_charging(vehicle, charge_state=charge_state, charge_session_kwh=41.37, charge_session_mi=142.0, session_minutes_remaining=95)
    assert vehicle.charge_session_energy.value is None
    assert vehicle.charging.estimated_date_reached.value is None


def test_an_absent_charging_block_publishes_no_session(vehicle):
    Connector._apply_charging(vehicle, pb.VehicleState(), MEASURED)
    assert vehicle.charge_session_energy.value is None and vehicle.charging.estimated_date_reached.value is None


@pytest.mark.parametrize("minutes", [0, 7 * 24 * 60 + 1, 65535, 65536, 4294967295])
def test_a_finish_time_needs_a_plausible_count(minutes):
    assert m.charge_finish(MEASURED, CHARGING, minutes) is None


def test_65535_minutes_is_no_estimate_even_while_charging(vehicle):
    """The real reading sent 65535; parked it is moot, but charging it would have been 45 days."""
    Connector._apply_charging(vehicle, gravity_reading().state, MEASURED)
    assert vehicle.charging.estimated_date_reached.value is None
    apply_charging(vehicle, at=later(1), charge_state=CHARGING, charge_session_kwh=4.0, session_minutes_remaining=65535)
    assert vehicle.charging.estimated_date_reached.value is None


@pytest.mark.parametrize("value,want", [(25, 25.0), (65534, 65534.0), (65535, None), (70000, None), (0, None), (None, None)])
def test_minutes_count_treats_the_uint16_ceiling_as_no_estimate(value, want):
    assert m.minutes_count(value) == want


def test_the_cable_latch_lands_in_the_standard_slot(vehicle):
    from carconnectivity.charging_connector import ChargingConnector as C

    apply_charging(vehicle, charge_state=CHARGING, cable_lock=pb.LOCK_STATE_LOCKED)
    assert vehicle.charging.connector.lock_state.value is C.ChargingConnectorLockState.LOCKED
    apply_charging(vehicle, at=later(1), charge_state=CHARGING, cable_lock=pb.LOCK_STATE_UNLOCKED)
    assert vehicle.charging.connector.lock_state.value is C.ChargingConnectorLockState.UNLOCKED
    apply_charging(vehicle, at=later(2), charge_state=CHARGING)
    assert vehicle.charging.connector.lock_state.value is C.ChargingConnectorLockState.UNKNOWN, "0 is the car not saying, not unlocked"


@pytest.mark.parametrize("charge_state", [pb.CHARGE_STATE_NOT_CONNECTED, pb.CHARGE_STATE_UNKNOWN])
def test_no_cable_no_latch_state(vehicle, charge_state):
    apply_charging(vehicle, charge_state=charge_state, cable_lock=pb.LOCK_STATE_LOCKED)
    assert vehicle.charging.connector.lock_state.value is None, "an empty port is not a locked connector"
    Connector._apply_charging(vehicle, gravity_reading().state, later(1))
    assert vehicle.charging.connector.lock_state.value is None


# ---- position ------------------------------------------------------------------

def gps_state(**gps):
    return pb.VehicleState(power=pb.POWER_STATE_DRIVE, gps=pb.Gps(location=pb.Location(latitude=39.7817, longitude=-89.6501), **gps))


def test_elevation_is_not_published_while_its_unit_is_unknown(vehicle):
    """The real reading: 8932 at a spot where other cars record 123 m."""
    Connector._apply_position(vehicle, gravity_reading().state, MEASURED)
    assert vehicle.position.latitude.value == pytest.approx(39.7817)
    assert vehicle.position.altitude.value is None


def test_the_raw_elevation_is_published_as_sent_for_calibration(vehicle):
    """The real reading's 8932, unitless and untouched, beside an empty position.altitude."""
    Connector._apply_position(vehicle, gravity_reading().state, MEASURED)
    assert vehicle.gps_elevation_raw.value == 8932.0
    assert vehicle.gps_elevation_raw.unit is None
    Connector._apply_position(vehicle, gps_state(elevation=-40), later(1))
    assert vehicle.gps_elevation_raw.value == -40.0, "negative is a value, whatever the unit"


@pytest.mark.parametrize("state", [pb.VehicleState(power=pb.POWER_STATE_DRIVE),
                                   pb.VehicleState(gps=pb.Gps(location=pb.Location(latitude=0.0, longitude=0.0), elevation=8932)),
                                   pb.VehicleState(gps=pb.Gps(location=pb.Location(latitude=39.7817, longitude=-89.6501)))])
def test_no_gps_no_fix_or_no_elevation_publishes_no_raw_elevation(vehicle, state):
    Connector._apply_position(vehicle, gravity_reading().state, MEASURED)
    Connector._apply_position(vehicle, state, later(1))
    assert vehicle.gps_elevation_raw.value is None, "0 is proto3's unset, and no fix means no elevation"


def test_an_absent_gps_block_is_not_a_position_at_zero_zero(vehicle):
    """A real VehicleState without gps reads 0.0, 0.0, which used to be published."""
    Connector._apply_position(vehicle, pb.VehicleState(power=pb.POWER_STATE_DRIVE), MEASURED)
    assert vehicle.position.latitude.value is None


# ---- security ----------------------------------------------------------------------

@pytest.mark.parametrize("table,proto_enum", [(m.ALARM_STATUS, pb.AlarmStatus), (m.LIVING_OBJECT_DETECTION, pb.LivingObjectDetectionStatus),
                                              (m.UNATTENDED_OCCUPANT_DETECTION, pb.UnattendedOccupantDetectionStatus)])
def test_each_table_follows_the_proto_enum(table, proto_enum):
    """The tables count up from 1 in the order the connector's enums are declared; tie
    every entry to the proto's own name so a reordering cannot shift them silently."""
    assert set(table) == set(proto_enum.values()) - {0}
    for number, ours in table.items():
        theirs = proto_enum.Name(number).replace("PANIC_MODE", "PANIC").replace("_WARNING", "")
        assert theirs.endswith(ours.name), (number, theirs, ours)


def apply_security(vehicle, st, at=MEASURED):
    Connector._apply_security(vehicle, st, at)
    Connector._apply_controls(vehicle, st, at)


def test_the_alarm_going_off_is_published_with_why(vehicle):
    apply_security(vehicle, pb.VehicleState(alarm=pb.AlarmState(mode=pb.ALARM_MODE_ON, status=pb.ALARM_STATUS_INTRUSION)))
    assert str(vehicle.alarm_status.value) == "intrusion" and str(vehicle.alarm.value) == "on"
    apply_security(vehicle, pb.VehicleState(alarm=pb.AlarmState(mode=pb.ALARM_MODE_ON, status=pb.ALARM_STATUS_ARMED)), later(1))
    assert str(vehicle.alarm_status.value) == "armed"
    apply_security(vehicle, pb.VehicleState(), later(2))
    assert vehicle.alarm_status.value is None, "no alarm block is not disarmed"


def test_an_armed_idle_sentry_is_not_published_as_not_recording(vehicle):
    """The real reading: Sentry IDLE (armed), is_active false, every reason false, USB in."""
    apply_security(vehicle, gravity_reading().state)
    Connector._apply_cabin(vehicle, gravity_reading().state, MEASURED)
    assert not hasattr(vehicle, "sentry_active"), "is_active false is the normal armed state; nothing is published from it"
    assert vehicle.sentry_inactive_reason.value is None
    assert vehicle.sentry_usb_drive_connected.value is True
    assert vehicle.sentry.value is True, "IDLE is armed, as before"


def test_a_reason_the_car_gives_is_published(vehicle):
    full = pb.SentryState(sentry_active_state=pb.SentryActiveState(inactive_reason_usb_full=True, inactive_reason_at_home=True))
    apply_security(vehicle, pb.VehicleState(sentry_state=full))
    assert str(vehicle.sentry_inactive_reason.value) == "at-home", "the first flag in proto order"
    apply_security(vehicle, gravity_reading().state, later(1))
    assert vehicle.sentry_inactive_reason.value is None, "no flag set, no reason"


@pytest.mark.parametrize("st", [pb.VehicleState(), pb.VehicleState(sentry_state=pb.SentryState(sentry_active_state=pb.SentryActiveState())),
                                pb.VehicleState(sentry_state=pb.SentryState(sentry_active_state=pb.SentryActiveState(
                                    is_active=True, inactive_reason_low_soc=True)))])
def test_no_reason_from_an_empty_absent_or_active_block(vehicle, st):
    apply_security(vehicle, st)
    assert vehicle.sentry_inactive_reason.value is None


@pytest.mark.parametrize("status,want", [(pb.SENTRY_USB_DRIVE_CONNECTED, True), (pb.SENTRY_USB_DRIVE_NOT_CONNECTED, False),
                                         (pb.SENTRY_USB_DRIVE_IDLE, None), (pb.UNKNOWN_SENTRY_USB_DRIVE_STATUS, None)])
def test_sentry_usb_drive(vehicle, status, want):
    apply_security(vehicle, pb.VehicleState(sentry_state=pb.SentryState(usb_drive_status=status)))
    assert vehicle.sentry_usb_drive_connected.value is want


def test_occupant_warnings_are_published_by_level(vehicle):
    body = pb.BodyState(living_object_detection_status=pb.LIVING_OBJECT_DETECTION_STATUS_LEVEL_2_WARNING,
                        unattended_occupant_detection_status=pb.UNATTENDED_OCCUPANT_DETECTION_STATUS_STAGE_3)
    apply_security(vehicle, pb.VehicleState(body=body))
    assert str(vehicle.living_object_detection.value) == "level-2"
    assert str(vehicle.unattended_occupant_detection.value) == "stage-3"
    apply_security(vehicle, pb.VehicleState(body=pb.BodyState()), later(1))
    assert vehicle.living_object_detection.value is None and vehicle.unattended_occupant_detection.value is None


@pytest.mark.parametrize("status,want", [(pb.KEYFOB_BATTERY_STATUS_LOW, True), (pb.KEYFOB_BATTERY_STATUS_SUFFICIENT, False),
                                         (pb.KEYFOB_BATTERY_STATUS_UNKNOWN, None)])
def test_keyfob_battery(vehicle, status, want):
    apply_security(vehicle, pb.VehicleState(body=pb.BodyState(keyfob_battery_status=status)))
    assert vehicle.keyfob_battery_low.value is want


def test_an_alarm_status_from_a_newer_proto_is_unknown():
    assert m.pick(m.ALARM_STATUS, 99) is None and m.pick(m.ALARM_STATUS, None) is None


# ---- window heating ------------------------------------------------------------------

def test_defrost_and_the_rear_window_fill_window_heatings(vehicle):
    from carconnectivity.window_heating import WindowHeatings as W

    Connector._apply_window_heating(vehicle, gravity_reading().state, MEASURED)  # the real reading: both off
    assert vehicle.window_heatings.windows["front"].heating_state.value is W.HeatingState.OFF
    assert vehicle.window_heatings.windows["rear"].heating_state.value is W.HeatingState.OFF
    assert vehicle.window_heatings.heating_state.value is W.HeatingState.OFF

    Connector._apply_window_heating(vehicle, pb.VehicleState(hvac=pb.HvacState(defrost=pb.DEFROST_ON,
                                                                               rear_window_heating_status=pb.REAR_WINDOW_HEATING_STATUS_OFF)), later(1))
    assert vehicle.window_heatings.windows["front"].heating_state.value is W.HeatingState.ON
    assert vehicle.window_heatings.heating_state.value is W.HeatingState.ON, "one heater on is heating"


@pytest.mark.parametrize("rear", [pb.REAR_WINDOW_HEATING_STATUS_UNKNOWN, pb.REAR_WINDOW_HEATING_STATUS_OFF_LOST_COMM_WITH_DCM,
                                  pb.REAR_WINDOW_HEATING_STATUS_ON_LOST_COMM_WITH_DCM])
def test_a_rear_heater_that_cannot_say_makes_the_summary_unknown(vehicle, rear):
    from carconnectivity.window_heating import WindowHeatings as W

    Connector._apply_window_heating(vehicle, gravity_reading().state, MEASURED)
    Connector._apply_window_heating(vehicle, pb.VehicleState(hvac=pb.HvacState(defrost=pb.DEFROST_OFF, rear_window_heating_status=rear)), later(1))
    assert vehicle.window_heatings.windows["rear"].heating_state.value is W.HeatingState.UNKNOWN, "lost comm is a stale reading"
    assert vehicle.window_heatings.heating_state.value is W.HeatingState.UNKNOWN, "front off is not all off"
    Connector._apply_window_heating(vehicle, pb.VehicleState(hvac=pb.HvacState(defrost=pb.DEFROST_ON, rear_window_heating_status=rear)), later(2))
    assert vehicle.window_heatings.heating_state.value is W.HeatingState.ON, "one heater on is heating whatever the other says"


def test_no_hvac_block_is_unknown_and_invents_no_heaters(vehicle):
    from carconnectivity.window_heating import WindowHeatings as W

    Connector._apply_window_heating(vehicle, pb.VehicleState(), MEASURED)
    assert vehicle.window_heatings.heating_state.value is W.HeatingState.UNKNOWN
    assert not vehicle.window_heatings.windows


# ---- battery health level --------------------------------------------------------------

def test_battery_health_level_is_the_cars_raw_number(vehicle):
    Connector._apply_cabin(vehicle, pb.VehicleState(battery=pb.BatteryState(battery_health_level=97.25)), MEASURED)
    assert vehicle.battery_health_level.value == pytest.approx(97.25)
    Connector._apply_cabin(vehicle, pb.VehicleState(battery=pb.BatteryState()), later(1))
    assert vehicle.battery_health_level.value is None, "0 is the car not saying, not a dead pack"


# ---- capability flags ----------------------------------------------------------------------

def test_a_config_that_says_nothing_refuses_nothing():
    from carconnectivity_connectors.lucid.vehicle import SeatClimateCommand as S

    config = pb.VehicleConfig()
    for seat in S.Seat:
        for mode in S.Mode:
            assert m.seat_refusal(config, seat, mode) is None
    assert m.steering_heater_refusal(config) is None and m.frunk_close_refusal(config) is None
    assert m.seat_refusal(None, S.Seat.DRIVER, S.Mode.HEAT) is None, "no config at all"


def test_the_proto_names_behind_the_capability_numbers():
    assert pb.FRONT_SEATS_VENTILATION_UNAVAILABLE == pb.HEATED_STEERING_WHEEL_UNAVAILABLE == m.FEATURE_UNAVAILABLE
    assert pb.SECOND_ROW_HEATED_SEATS_UNAVAILABLE == pb.FRONT_SEATS_HEATING_UNAVAILABLE == m.FEATURE_UNAVAILABLE
    assert pb.REAR_SEAT_CONFIG_6_SEAT == m.REAR_SEATS_SIX and pb.STRUT_TYPE_GAS == m.STRUT_GAS
    config = pb.VehicleConfig(frunk_strut=pb.STRUT_TYPE_GAS, heated_steering_wheel=pb.HEATED_STEERING_WHEEL_UNAVAILABLE)
    assert m.frunk_close_refusal(config) and m.steering_heater_refusal(config)


def test_the_real_reading_has_no_update_waiting(vehicle):
    Connector._apply_cabin(vehicle, gravity_reading().state, MEASURED)
    assert vehicle.software_update_available.value is None and vehicle.software_update_version.value is None
    assert vehicle.software_update_install_time.value is None


def test_the_keyless_and_valet_numbers_are_the_protos_own():
    """mapping keeps the enum numbers it reads as plain constants (it imports no protobuf); these
    tie them to the library's enums, so a renumbering upstream fails here, not on a car."""
    from lucidmotors.gen import vehicle_state_service_pb2 as vs

    from carconnectivity_connectors.lucid import mapping

    assert mapping.KEYLESS_ON == {vs.KEYLESS_DRIVING_ON} and mapping.KEYLESS_OFF == {vs.KEYLESS_DRIVING_OFF}
    assert mapping.DRIVE_MODE_VALET == vs.DRIVE_MODE_VALET
