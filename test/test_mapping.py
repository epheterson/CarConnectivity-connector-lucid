"""Pinned to the 2026-08-10 05:59 UTC snapshot of a real Gravity (vehicles.db row 1)."""
from carconnectivity.charging import Charging
from carconnectivity.doors import Doors
from carconnectivity.position import Position
from carconnectivity.vehicle import GenericVehicle

from carconnectivity_connectors.lucid import mapping as m


def test_snapshot_parked_asleep_not_connected():
    # power SLEEP(1), charge NOT_CONNECTED(1), speed 0.0, door_locks 2
    assert m.is_awake(1, 1) is False
    assert m.vehicle_state(1, 1) is GenericVehicle.State.OFFLINE
    assert m.charging_state(1) is Charging.ChargingState.OFF
    assert m.charging_type(0, 1) is Charging.ChargingType.OFF
    assert m.lock_state(2) is Doors.LockState.LOCKED
    assert m.position_type(1) is Position.PositionType.PARKING


def test_charging_overrides_sleep():
    # observed 2026-08-11: SLEEP_CHARGE(6) for a whole session; must not be "asleep"
    assert m.is_awake(6, 8) is True
    assert m.charging_state(8) is Charging.ChargingState.CHARGING
    assert m.vehicle_state(6, 8) is GenericVehicle.State.PARKED


def test_undefined_power_states_are_not_awake():
    assert m.is_awake(12) is False
    assert m.is_awake(13) is False


def test_driving():
    assert m.vehicle_state(4, 1) is GenericVehicle.State.DRIVING
    assert m.position_type(4) is Position.PositionType.DRIVING


def test_speed_is_metres_per_second_in_the_proto():
    assert m.speed_kmh(10.0) == 36.0
    assert m.speed_kmh(None) is None


def test_charge_end_and_faults():
    assert m.charging_state(9) is Charging.ChargingState.CONSERVATION
    assert m.charging_state(13) is Charging.ChargingState.READY_FOR_CHARGING  # CHARGING_STOPPED: still plugged in
    assert m.charging_state(30) is Charging.ChargingState.READY_FOR_CHARGING  # CHARGING_SCHEDULED
    assert m.charging_state(11) is Charging.ChargingState.ERROR
    assert m.charging_state(19) is Charging.ChargingState.DISCHARGING


def test_temperature_plausibility():
    assert m.plausible_temp(26.8) == 26.8
    assert m.plausible_temp(109.6) is None
    assert m.plausible_temp(None) is None


def test_doors():
    assert m.door_open_state(2) is Doors.OpenState.CLOSED
    assert m.door_open_state(1) is Doors.OpenState.OPEN
    assert set(m.DOORS) == {"front_left", "front_right", "rear_left", "rear_right", "frunk", "trunk"}


def test_hvac_power_two_is_off():
    # proto: HVAC_ON = 1, HVAC_OFF = 2. The bridge had this inverted.
    assert m.hvac_active(1) is True
    assert m.hvac_active(2) is False
    assert m.hvac_active(3) is True   # PRECONDITION
    assert m.hvac_active(6) is True   # KEEP_TEMP
    assert m.hvac_active(0) is None
    assert m.hvac_active(None) is None


def test_door_ajar_and_close_error():
    assert m.door_open_state(3) is Doors.OpenState.AJAR
    assert m.door_open_state(4) is Doors.OpenState.OPEN
    assert m.door_open_state(0) is Doors.OpenState.UNKNOWN


def test_energy_none_is_off():
    assert m.charging_type(5, 8) is Charging.ChargingType.OFF
