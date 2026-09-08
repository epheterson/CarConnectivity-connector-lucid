"""The parts of mapping.py that had no test: the None-safe spine, charge type, locks,
position type and the speed unit.

These are the functions where a wrong answer is silent. Nothing raises when a charge is
labelled AC instead of DC, or when metres per second are published as kilometres per
hour — the number simply reads plausibly and is wrong, and the first person to notice is
whoever compares a dashboard against the car months later.
"""

import pytest
from carconnectivity.charging import Charging
from carconnectivity.doors import Doors
from carconnectivity.position import Position

from carconnectivity_connectors.lucid import mapping as m

# ---- the None-safe spine ---------------------------------------------------


class Node:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_dig_walks_a_path_and_stops_at_the_first_gap():
    tree = Node(chassis=Node(speed=13.5))
    assert m._dig(tree, "chassis", "speed") == 13.5
    assert m._dig(tree, "chassis", "missing") is None
    assert m._dig(tree, "missing", "speed") is None, "a gap halfway must not raise"
    assert m._dig(None, "chassis") is None


def test_dig_does_not_confuse_a_real_zero_with_a_gap():
    # A pressure or a speed of exactly zero is a reading, not a missing field.
    assert m._dig(Node(chassis=Node(speed=0.0)), "chassis", "speed") == 0.0


@pytest.mark.parametrize(
    "value,want",
    [
        (1, 1.0),
        (1.5, 1.5),
        ("2.5", 2.5),
        (None, None),
        ("", None),
        ("abc", None),
        ([], None),
        (Node(), None),
    ],
)
def test_f_returns_a_float_or_nothing(value, want):
    assert m._f(value) == want


# ---- charging type ---------------------------------------------------------


def test_an_unplugged_car_is_not_charging_whatever_the_energy_type_says():
    # ChargeState wins: the energy type lingers at its last value after unplugging,
    # so trusting it alone leaves a car reading AC while it sits in a car park.
    for cs in m.CHARGE_NOT_CONNECTED:
        for energy in (m.ENERGY_AC, m.ENERGY_DC, None, 0):
            assert m.charging_type(energy, cs) is Charging.ChargingType.OFF, (
                energy,
                cs,
            )


def test_ac_and_dc_are_told_apart_while_connected():
    assert m.charging_type(m.ENERGY_AC, 8) is Charging.ChargingType.AC
    assert m.charging_type(m.ENERGY_DC, 8) is Charging.ChargingType.DC
    assert m.charging_type(m.ENERGY_NONE, 8) is Charging.ChargingType.OFF


def test_an_energy_type_we_have_not_seen_is_unknown_not_a_guess():
    # 3 DIGITAL and 4 V2V exist in the proto and have never been observed. Reporting
    # them as AC would put invented kilowatt-hours into somebody's cost report.
    for energy in (0, 3, 4, 99, None):
        assert m.charging_type(energy, 8) is Charging.ChargingType.UNKNOWN, energy


# ---- charging state --------------------------------------------------------


def test_every_enumerated_charge_state_maps_where_it_should():
    assert m.charging_state(8) is Charging.ChargingState.CHARGING
    assert m.charging_state(0) is Charging.ChargingState.OFF
    assert m.charging_state(19) is Charging.ChargingState.DISCHARGING
    assert m.charging_state(9) is Charging.ChargingState.CONSERVATION
    for cs in m.CHARGE_ERROR:
        assert m.charging_state(cs) is Charging.ChargingState.ERROR, cs


def test_an_unenumerated_charge_state_is_plugged_in_rather_than_off():
    # The comment in mapping.py records why: an allowlist of connected states once
    # missed seventeen values, CHARGING_STOPPED and every fault code among them, and
    # those cars read as unplugged.
    for cs in (2, 3, 13, 17, 20, 25, 30):
        assert m.charging_state(cs) is Charging.ChargingState.READY_FOR_CHARGING, cs
    assert m.charging_state(None) is Charging.ChargingState.UNKNOWN


# ---- locks -----------------------------------------------------------------


def test_only_the_locked_value_means_locked():
    assert m.lock_state(m.LOCK_LOCKED) is Doors.LockState.LOCKED
    assert m.lock_state(m.LOCK_UNLOCKED) is Doors.LockState.UNLOCKED
    assert m.lock_state(None) is Doors.LockState.UNKNOWN


def test_an_unknown_lock_state_is_not_reported_as_unlocked():
    # Proto 0 is UNKNOWN. It used to fall through to UNLOCKED, and the direction of that
    # mistake matters: anything watching locked -> unlocked raises an alarm about a car
    # left open, on a car that had simply not said yet.
    assert m.lock_state(m.LOCK_UNKNOWN) is Doors.LockState.UNKNOWN


def test_a_door_that_failed_to_close_is_not_closed():
    assert m.door_open_state(m.DOOR_CLOSE_ERROR) is Doors.OpenState.OPEN
    assert m.door_open_state(m.DOOR_CLOSED) is Doors.OpenState.CLOSED
    assert m.door_open_state(m.DOOR_AJAR) is Doors.OpenState.AJAR
    assert m.door_open_state(0) is Doors.OpenState.UNKNOWN


# ---- position and speed ----------------------------------------------------


def test_position_type_is_driving_only_while_driving():
    assert m.position_type(m.POWER_DRIVE) is Position.PositionType.DRIVING
    for state in (
        m.POWER_SLEEP,
        m.POWER_WINK,
        m.POWER_ACCESSORY,
        m.POWER_LIVE_CHARGE,
        None,
    ):
        assert m.position_type(state) is Position.PositionType.PARKING, state


def test_speed_is_converted_from_metres_per_second():
    # The proto comment says metres per second and the field is named plainly enough
    # to be mistaken for km/h. Publishing it unconverted understates a motorway speed
    # by a factor of 3.6, which still looks like a speed.
    assert m.speed_kmh(0.0) == 0.0
    assert m.speed_kmh(1.0) == pytest.approx(3.6)
    assert m.speed_kmh(31.3) == pytest.approx(112.68), "31.3 m/s is about 70 mph"
    assert m.speed_kmh(None) is None
    assert m.speed_kmh("not a number") is None


# ---- temperature -----------------------------------------------------------


def test_an_impossible_temperature_is_dropped_but_a_cold_one_is_kept():
    assert m.plausible_temp(109.6) is None, "the spike that prompted the check"
    assert m.plausible_temp(-273.0) is None
    assert m.plausible_temp(m.TEMP_MIN_C) == m.TEMP_MIN_C, "the bounds are inclusive"
    assert m.plausible_temp(m.TEMP_MAX_C) == m.TEMP_MAX_C
    assert m.plausible_temp(-40.0) == -40.0
    assert m.plausible_temp(0.0) == 0.0, "freezing is a real temperature, not a missing one"
    assert m.plausible_temp(None) is None


# ---- the door map ----------------------------------------------------------


def test_every_door_name_points_at_a_distinct_field():
    assert len(set(m.DOORS.values())) == len(m.DOORS), "two doors reading the same field"
    assert set(m.DOORS) == {
        "front_left",
        "front_right",
        "rear_left",
        "rear_right",
        "frunk",
        "trunk",
    }


# ---- how often to poll ------------------------------------------------------


def test_a_moving_car_is_polled_more_often():
    from carconnectivity.vehicle import GenericVehicle

    assert m.poll_interval([GenericVehicle.State.DRIVING], 60, 15) == 15
    assert m.poll_interval([GenericVehicle.State.IGNITION_ON], 60, 15) == 15, "about to move counts; the first fix of a drive is the one you cannot go back for"


def test_a_car_that_is_not_going_anywhere_is_left_alone():
    from carconnectivity.vehicle import GenericVehicle

    for state in (GenericVehicle.State.OFFLINE, GenericVehicle.State.PARKED, None):
        assert m.poll_interval([state], 60, 15) == 60, state
    assert m.poll_interval([], 60, 15) == 60, "no vehicles is not a reason to poll fast"


def test_one_moving_car_sets_the_pace_for_the_account():
    from carconnectivity.vehicle import GenericVehicle

    # One request returns every vehicle, so the fastest car decides.
    assert m.poll_interval([GenericVehicle.State.PARKED, GenericVehicle.State.DRIVING], 60, 15) == 15
