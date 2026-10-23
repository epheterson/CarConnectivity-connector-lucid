"""_apply_drive maps Lucid battery fields onto the CarConnectivity drive/battery model.

Regression for v0.1.1: Lucid's `kwhr` is energy remaining at the current state of
charge, not capacity. It used to land in `available_capacity`, which the database
plugin stores as `drives.capacity` and the dashboards multiply SoC deltas by.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from carconnectivity.carconnectivity import CarConnectivity
from carconnectivity_connectors.lucid.connector import Connector
from carconnectivity_connectors.lucid.vehicle import LucidElectricVehicle


@pytest.fixture
def vehicle(tmp_path):
    cc = CarConnectivity(config={"carConnectivity": {}}, tokenstore_file=str(tmp_path / "tokens"),
                         cache_file=str(tmp_path / "cache"))
    try:
        yield LucidElectricVehicle(vin="TESTVIN0000000001", garage=cc.garage)
    finally:
        cc.shutdown()


def lucid_state(**battery):
    return SimpleNamespace(battery=SimpleNamespace(**battery))


def test_usable_capacity_comes_from_capacity_kwhr_not_remaining_energy(vehicle):
    measured = datetime(2026, 9, 3, 22, 0, tzinfo=timezone.utc)
    st = lucid_state(charge_percent=74.7, remaining_range=465.0, capacity_kwhr=117.21, kwhr=87.53,
                     min_cell_temp=21.0, max_cell_temp=23.5)

    Connector._apply_drive(vehicle, st, measured)

    drive = vehicle.drives.drives["primary"]
    assert drive.level.value == pytest.approx(74.7)
    assert drive.battery.available_capacity.value == pytest.approx(117.21)
    assert drive.battery.total_capacity.value is None, "the car does not report gross capacity"
    assert drive.battery.available_capacity.value != pytest.approx(87.53), "remaining energy must not be reported as capacity"


def test_missing_capacity_leaves_the_field_unset(vehicle):
    Connector._apply_drive(vehicle, lucid_state(charge_percent=50.0, remaining_range=300.0), datetime.now(tz=timezone.utc))
    drive = vehicle.drives.drives["primary"]
    assert drive.battery.available_capacity.value is None
    assert drive.level.value == pytest.approx(50.0)


def test_doors_with_nothing_to_report_are_not_summarised_as_closed(vehicle):
    """A body block the car did not send used to publish "all doors closed".

    The loop skips every absent door, so `any_open` stayed False and the aggregate was set
    to CLOSED — a reassuring answer invented from no data at all, indistinguishable from a
    car that really did report six shut doors.
    """
    from carconnectivity.doors import Doors

    Connector._apply_doors(vehicle, SimpleNamespace(body=None), datetime.now(tz=timezone.utc))

    assert vehicle.doors.open_state.value is Doors.OpenState.UNKNOWN
    assert vehicle.doors.lock_state.value is Doors.LockState.UNKNOWN


def test_doors_that_do_report_are_still_summarised(vehicle):
    from carconnectivity.doors import Doors

    body = SimpleNamespace(door_locks=2, front_left_door=2, front_right_door=2, rear_left_door=2,
                           rear_right_door=2, front_cargo=2, rear_cargo=2)
    Connector._apply_doors(vehicle, SimpleNamespace(body=body), datetime.now(tz=timezone.utc))

    assert vehicle.doors.open_state.value is Doors.OpenState.CLOSED
    assert vehicle.doors.lock_state.value is Doors.LockState.LOCKED

    body.rear_left_door = 3  # ajar
    Connector._apply_doors(vehicle, SimpleNamespace(body=body), datetime.now(tz=timezone.utc))
    assert vehicle.doors.open_state.value is Doors.OpenState.OPEN


def test_speed_is_published_in_kilometres_per_hour(vehicle):
    """The car reports speed and CarConnectivity's model has nowhere for it, so it hangs
    on the Lucid vehicle as a connector-specific attribute. chassis.speed is km/h already,
    whatever the proto's annotation says; converting it as metres per second overstated a
    neighbourhood drive by 3.6 and still looked like a speed."""
    Connector._apply_speed(vehicle, SimpleNamespace(chassis=SimpleNamespace(speed=112.68)), datetime.now(tz=timezone.utc))
    assert vehicle.speed.value == pytest.approx(112.68)
    assert vehicle.speed.unit.value == "km/h"


def test_a_parked_car_reports_zero_rather_than_nothing(vehicle):
    Connector._apply_speed(vehicle, SimpleNamespace(chassis=SimpleNamespace(speed=0.0)), datetime.now(tz=timezone.utc))
    assert vehicle.speed.value == 0.0


def test_no_chassis_leaves_speed_unset(vehicle):
    Connector._apply_speed(vehicle, SimpleNamespace(chassis=None), datetime.now(tz=timezone.utc))
    assert vehicle.speed.value is None


def test_windows_are_published_per_window_and_summarised(vehicle):
    from carconnectivity.windows import Windows

    wp = SimpleNamespace(left_front=1, left_rear=1, right_front=1, right_rear=1)
    Connector._apply_windows(vehicle, SimpleNamespace(body=SimpleNamespace(window_position=wp)), datetime.now(tz=timezone.utc))
    assert vehicle.windows.open_state.value is Windows.OpenState.CLOSED
    assert set(vehicle.windows.windows) == {"front_left", "front_right", "rear_left", "rear_right"}
    wp.right_rear = 12  # vent drop
    Connector._apply_windows(vehicle, SimpleNamespace(body=SimpleNamespace(window_position=wp)), datetime.now(tz=timezone.utc))
    assert vehicle.windows.windows["rear_right"].open_state.value is Windows.OpenState.AJAR
    assert vehicle.windows.open_state.value is Windows.OpenState.OPEN, "one window down is windows down"


def test_no_window_block_is_unknown_not_all_closed(vehicle):
    from carconnectivity.windows import Windows

    Connector._apply_windows(vehicle, SimpleNamespace(body=None), datetime.now(tz=timezone.utc))
    assert vehicle.windows.open_state.value is Windows.OpenState.UNKNOWN


def test_a_window_that_cannot_say_keeps_the_summary_from_saying_closed(vehicle):
    """A close with three windows mid-travel read back as closed: the driver's said
    FULLY_CLOSED and the others UNKNOWN, and only open ones counted against closed."""
    from carconnectivity.windows import Windows

    wp = SimpleNamespace(left_front=1, left_rear=6, right_front=0, right_rear=6)
    Connector._apply_windows(vehicle, SimpleNamespace(body=SimpleNamespace(window_position=wp)), datetime.now(tz=timezone.utc))
    assert vehicle.windows.open_state.value is Windows.OpenState.UNKNOWN
    wp.right_rear = 5
    Connector._apply_windows(vehicle, SimpleNamespace(body=SimpleNamespace(window_position=wp)), datetime.now(tz=timezone.utc))
    assert vehicle.windows.open_state.value is Windows.OpenState.OPEN, "one down is down, whatever the rest say"


def test_plug_state_is_published_with_the_charge(vehicle):
    from carconnectivity.charging import ChargingConnector as C

    ch = SimpleNamespace(charge_state=8, energy_type=1, charge_rate_kwh_precise=11.2, charge_rate_mph_precise=30.0, charge_limit_percent=80)
    Connector._apply_charging(vehicle, SimpleNamespace(charging=ch), datetime.now(tz=timezone.utc))
    assert vehicle.charging.connector.connection_state.value is C.ChargingConnectorConnectionState.CONNECTED
    assert vehicle.charging.connector.external_power.value is C.ExternalPower.ACTIVE
    ch.charge_state = 1
    Connector._apply_charging(vehicle, SimpleNamespace(charging=ch), datetime.now(tz=timezone.utc))
    assert vehicle.charging.connector.connection_state.value is C.ChargingConnectorConnectionState.DISCONNECTED
    assert vehicle.charging.connector.external_power.value is C.ExternalPower.UNAVAILABLE


def test_cabin_temperature_and_sentry_are_published(vehicle):
    st = SimpleNamespace(cabin=SimpleNamespace(interior_temp=23.4, exterior_temp=22.7), sentry_state=SimpleNamespace(enablement_state=1))
    Connector._apply_cabin(vehicle, st, datetime.now(tz=timezone.utc))
    assert vehicle.inside_temperature.value == pytest.approx(23.4)
    assert vehicle.sentry.value is True
    st.sentry_state.enablement_state = 3
    st.cabin.interior_temp = 109.6  # the exterior sensor once said this for two minutes; the same bound applies
    Connector._apply_cabin(vehicle, st, datetime.now(tz=timezone.utc))
    assert vehicle.sentry.value is True and vehicle.inside_temperature.value is None  # IDLE is armed
    st.sentry_state.enablement_state = 2
    Connector._apply_cabin(vehicle, st, datetime.now(tz=timezone.utc))
    assert vehicle.sentry.value is False


def test_tire_pressures_and_the_cars_warning_are_published(vehicle):
    ch = SimpleNamespace(front_left_tire_pressure_bar=2.85, front_right_tire_pressure_bar=2.85,
                         rear_left_tire_pressure_bar=2.85, rear_right_tire_pressure_bar=2.34,
                         hard_warn_left_front=1, hard_warn_left_rear=1, hard_warn_right_front=1, hard_warn_right_rear=1,
                         soft_warn_left_front=1, soft_warn_left_rear=1, soft_warn_right_front=1, soft_warn_right_rear=2)
    Connector._apply_tires(vehicle, SimpleNamespace(chassis=ch), datetime.now(tz=timezone.utc))
    assert vehicle.tire_pressure_rear_right.value == pytest.approx(2.34)
    assert vehicle.tire_pressure_front_left.value == pytest.approx(2.85)
    assert vehicle.tire_warning.value is True, "a soft warning on one wheel is a warning"
    ch.soft_warn_right_rear = 1
    Connector._apply_tires(vehicle, SimpleNamespace(chassis=ch), datetime.now(tz=timezone.utc))
    assert vehicle.tire_warning.value is False


def test_no_chassis_block_publishes_no_tire_claims(vehicle):
    Connector._apply_tires(vehicle, SimpleNamespace(chassis=None), datetime.now(tz=timezone.utc))
    assert vehicle.tire_pressure_front_left.value is None and vehicle.tire_warning.value is None


def test_threat_battery_and_update_are_published(vehicle):
    st = SimpleNamespace(cabin=SimpleNamespace(interior_temp=22.0), sentry_state=SimpleNamespace(enablement_state=3, threat_level=3),
                         battery=SimpleNamespace(capacity_kwhr=117.15, kwhr=92.63, battery_health=1), software_update=SimpleNamespace(update_available=True))
    Connector._apply_cabin(vehicle, st, datetime.now(tz=timezone.utc))
    assert vehicle.sentry_threat.value is True
    assert vehicle.battery_capacity_kwh.value == pytest.approx(117.15) and vehicle.battery_energy_kwh.value == pytest.approx(92.63)
    assert vehicle.battery_health_warning.value is False and vehicle.software_update_available.value is True


def test_halo_secure_state_is_published(vehicle):
    st = SimpleNamespace(sentry_state=SimpleNamespace(enhanced_deterrence_state=3), alarm=SimpleNamespace(mode=3))
    Connector._apply_controls(vehicle, st, datetime.now(tz=timezone.utc))
    assert vehicle.enhanced_deterrence.value is True, "IDLE is on, as it is for Sentry"
    assert str(vehicle.alarm.value) == "silent"
    st.sentry_state.enhanced_deterrence_state, st.alarm.mode = 2, 0
    Connector._apply_controls(vehicle, st, datetime.now(tz=timezone.utc))
    assert vehicle.enhanced_deterrence.value is False and vehicle.alarm.value is None


def test_keyless_driving_and_valet_are_published(vehicle):
    st = SimpleNamespace(keyless_driving=1, drive_mode=4)
    Connector._apply_controls(vehicle, st, datetime.now(tz=timezone.utc))
    assert vehicle.keyless_driving.value is True and vehicle.valet.value is True
    st.keyless_driving, st.drive_mode = 2, 1
    Connector._apply_controls(vehicle, st, datetime.now(tz=timezone.utc))
    assert vehicle.keyless_driving.value is False and vehicle.valet.value is False
    st.keyless_driving, st.drive_mode = 0, 0
    Connector._apply_controls(vehicle, st, datetime.now(tz=timezone.utc))
    assert vehicle.keyless_driving.value is None and vehicle.valet.value is None, "0 is the car not saying"


def test_charge_port_door_and_headlights_are_published(vehicle):
    from carconnectivity.doors import Doors
    from carconnectivity.lights import Lights

    st = SimpleNamespace(body=SimpleNamespace(charge_port=1), chassis=SimpleNamespace(headlights=2))
    Connector._apply_controls(vehicle, st, datetime.now(tz=timezone.utc))
    assert vehicle.charge_port.value is Doors.OpenState.OPEN
    assert vehicle.lights.light_state.value is Lights.LightState.ON
    assert vehicle.doors.open_state.value is not Doors.OpenState.OPEN, "a charge port door is not a door: charging is not 'doors open'"
    st.body.charge_port, st.chassis.headlights = 2, 3
    Connector._apply_controls(vehicle, st, datetime.now(tz=timezone.utc))
    assert vehicle.charge_port.value is Doors.OpenState.CLOSED and vehicle.lights.light_state.value is Lights.LightState.OFF


def test_climate_toggles_and_heater_levels_are_published(vehicle):
    seats = SimpleNamespace(driver_heat_backrest_zone1=3, driver_heat_backrest_zone3=5, driver_heat_cushion_zone2=0, driver_heat_cushion_zone4=4,
                            driver_vent_backrest=2, driver_vent_cushion=2, rear_passenger_heat_left=0)
    st = SimpleNamespace(hvac=SimpleNamespace(max_ac_status=2, keep_climate_status=4, steering_heater_level=3, seats=seats))
    Connector._apply_controls(vehicle, st, datetime.now(tz=timezone.utc))
    assert vehicle.max_ac.value is True and vehicle.creature_comfort.value is True
    assert str(vehicle.steering_wheel_heater.value) == "medium"
    assert str(vehicle.seat_climate["seat_heat_driver"].value) == "high", "a seat's level is its warmest zone"
    assert str(vehicle.seat_climate["seat_vent_driver"].value) == "off"
    assert vehicle.seat_climate["seat_heat_rear_left"].value is None, "0 is the car not saying"
    st.hvac.max_ac_status, st.hvac.keep_climate_status, st.hvac.steering_heater_level = 1, 2, 1
    Connector._apply_controls(vehicle, st, datetime.now(tz=timezone.utc))
    assert vehicle.max_ac.value is False and vehicle.creature_comfort.value is False, "keep climate on is not pet mode"
    assert str(vehicle.steering_wheel_heater.value) == "off"
    # the level is a setting the car keeps; the status says whether the wheel is warm
    st.hvac.steering_heater_level, st.hvac.steering_heater = 4, 1
    Connector._apply_controls(vehicle, st, datetime.now(tz=timezone.utc))
    assert str(vehicle.steering_wheel_heater.value) == "off", "a kept level on a heater that is off"
    st.hvac.steering_heater = 2
    Connector._apply_controls(vehicle, st, datetime.now(tz=timezone.utc))
    assert str(vehicle.steering_wheel_heater.value) == "high"


def test_battery_preconditioning_and_the_ac_current_limit_are_published(vehicle):
    st = SimpleNamespace(battery=SimpleNamespace(preconditioning_status=2),
                         charging=SimpleNamespace(charge_state=1, energy_ac_current_limit=40, active_session_ac_current_limit=0))
    Connector._apply_controls(vehicle, st, datetime.now(tz=timezone.utc))
    Connector._apply_charging(vehicle, st, datetime.now(tz=timezone.utc))
    assert vehicle.battery_preconditioning.value is True
    assert vehicle.charging.settings.maximum_current.value == 40
    st.battery.preconditioning_status, st.charging.energy_ac_current_limit = 3, 0
    Connector._apply_controls(vehicle, st, datetime.now(tz=timezone.utc))
    Connector._apply_charging(vehicle, st, datetime.now(tz=timezone.utc))
    assert vehicle.battery_preconditioning.value is False, "unavailable is not on"
    assert vehicle.charging.settings.maximum_current.value is None, "0 is the car not saying, not a 0 A limit"
