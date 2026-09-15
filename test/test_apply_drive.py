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
