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
