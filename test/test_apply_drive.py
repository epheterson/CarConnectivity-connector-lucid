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
