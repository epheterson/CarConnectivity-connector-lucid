"""Lucid protobuf state -> CarConnectivity vocabulary. Pure functions, no I/O.

Every conversion here is pinned by a test to a value observed on a real
Gravity, because unit bugs in this layer are invisible until someone eyeballs
a dashboard. Lucid reports km, km/h-looking-but-actually-m/s for speed
(proto comment: "in meters/second"), Celsius and bar.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from carconnectivity.charging import Charging
from carconnectivity.doors import Doors
from carconnectivity.position import Position
from carconnectivity.vehicle import GenericVehicle

# --- Lucid enum ints (proto/vehicle_state_service.proto) --------------------

# PowerState. 12 and 13 are undefined upstream (python-lucidmotors #23); on a
# Gravity 13 is entered from WINK/MONITOR/SLEEP_CHARGE and exits to SLEEP -- the
# car going down. Treat both as NOT awake.
POWER_SLEEP, POWER_WINK, POWER_ACCESSORY, POWER_DRIVE = 1, 2, 3, 4
POWER_LIVE_CHARGE, POWER_SLEEP_CHARGE, POWER_LIVE_UPDATE, POWER_SLEEP_UPDATE = 5, 6, 7, 8
POWER_CLOUD_1, POWER_CLOUD_2, POWER_MONITOR = 9, 10, 11
_ASLEEP = {POWER_SLEEP, POWER_SLEEP_CHARGE, POWER_SLEEP_UPDATE, POWER_CLOUD_1, POWER_CLOUD_2, 12, 13}

# ChargeState (0..30). Only the small stable sets are enumerated; everything
# else is "plugged in, not moving power". An allowlist of connected states once
# missed 17 values including CHARGING_STOPPED and every fault code.
CHARGE_NOT_CONNECTED = {0, 1}
CHARGE_CHARGING = {8}
CHARGE_END_OK = {9}
CHARGE_ERROR = {10, 11, 12, 15, 16, 22, 26, 27, 28, 29}
CHARGE_DISCHARGING = {19}

# EnergyType: 0 UNKNOWN, 1 AC, 2 DC, 3 DIGITAL, 4 V2V, 5 NONE (proto enum).
ENERGY_AC, ENERGY_DC, ENERGY_NONE = 1, 2, 5

# LockState: 0 UNKNOWN, 1 UNLOCKED, 2 LOCKED.  DoorState: 0 UNKNOWN, 1 OPEN, 2 CLOSED, 3 AJAR, 4 CLOSE_ERROR.
LOCK_UNKNOWN, LOCK_UNLOCKED, LOCK_LOCKED = 0, 1, 2
DOOR_OPEN, DOOR_CLOSED, DOOR_AJAR, DOOR_CLOSE_ERROR = 1, 2, 3, 4

# HvacPower: 0 UNKNOWN, 1 ON, 2 OFF, 3 PRECONDITION, 5 RESIDUAL_HEATING, 6 KEEP_TEMP, 7 HEATSTROKE_PREVENTION.
# NOTE: 2 is OFF. The lucid-bridge treated 2 as on; that is inverted.
HVAC_OFF = 2
_HVAC_ACTIVE = {1, 3, 5, 6, 7}

MPS_TO_KMH = 3.6
TEMP_MIN_C, TEMP_MAX_C = -60.0, 70.0  # a Gravity once reported 109.6 C exterior for two minutes


def _dig(obj: Any, *path: str) -> Any:
    for key in path:
        if obj is None:
            return None
        obj = getattr(obj, key, None)
    return obj


def _f(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def plausible_temp(c: Optional[float]) -> Optional[float]:
    """Drop physically impossible ambient readings; the car occasionally emits a transient spike."""
    if c is None:
        return None
    return c if TEMP_MIN_C <= c <= TEMP_MAX_C else None


def is_awake(power_state: Optional[int], charge_state: Optional[int] = None) -> bool:
    """A charging car is never asleep, whatever its power state says (observed:
    a Gravity sits in SLEEP_CHARGE for an entire session)."""
    if charge_state in CHARGE_CHARGING:
        return True
    if power_state is None:
        return False
    return power_state not in _ASLEEP


def vehicle_state(power_state: Optional[int], charge_state: Optional[int]) -> GenericVehicle.State:
    """CarConnectivity vehicle state from Lucid power + charge state."""
    if power_state == POWER_DRIVE:
        return GenericVehicle.State.DRIVING
    if not is_awake(power_state, charge_state):
        return GenericVehicle.State.OFFLINE
    if power_state == POWER_ACCESSORY:
        return GenericVehicle.State.IGNITION_ON
    return GenericVehicle.State.PARKED


_CHARGING_STATE_BY_SET = (
    (CHARGE_CHARGING, Charging.ChargingState.CHARGING),
    (CHARGE_NOT_CONNECTED, Charging.ChargingState.OFF),
    (CHARGE_DISCHARGING, Charging.ChargingState.DISCHARGING),
    (CHARGE_ERROR, Charging.ChargingState.ERROR),
    (CHARGE_END_OK, Charging.ChargingState.CONSERVATION),
)


def charging_state(cs: Optional[int]) -> Charging.ChargingState:
    """Lucid ChargeState int -> CarConnectivity ChargingState. Anything not enumerated is plugged in, not charging."""
    if cs is None:
        return Charging.ChargingState.UNKNOWN
    for members, state in _CHARGING_STATE_BY_SET:
        if cs in members:
            return state
    return Charging.ChargingState.READY_FOR_CHARGING


def charging_type(energy_type: Optional[int], cs: Optional[int]) -> Charging.ChargingType:
    """AC / DC / OFF from Lucid EnergyType and ChargeState."""
    if cs in CHARGE_NOT_CONNECTED:
        return Charging.ChargingType.OFF
    if energy_type == ENERGY_NONE:
        return Charging.ChargingType.OFF
    if energy_type == ENERGY_AC:
        return Charging.ChargingType.AC
    if energy_type == ENERGY_DC:
        return Charging.ChargingType.DC
    return Charging.ChargingType.UNKNOWN


def lock_state(door_locks: Optional[int]) -> Doors.LockState:
    """Lucid LockState int -> Doors.LockState.

    Proto 0 is UNKNOWN, not unlocked. It used to fall through to UNLOCKED, which is the
    dangerous direction: a car that reports 0 while waking would be published as unlocked,
    and anything watching for that transition sends a false alarm about an open car."""
    if door_locks is None or door_locks == LOCK_UNKNOWN:
        return Doors.LockState.UNKNOWN
    return Doors.LockState.LOCKED if door_locks == LOCK_LOCKED else Doors.LockState.UNLOCKED


def door_open_state(door: Optional[int]) -> Doors.OpenState:
    """Lucid DoorState int -> Doors.OpenState (AJAR preserved)."""
    if door is None or door == 0:
        return Doors.OpenState.UNKNOWN
    if door == DOOR_CLOSED:
        return Doors.OpenState.CLOSED
    if door == DOOR_AJAR:
        return Doors.OpenState.AJAR
    return Doors.OpenState.OPEN  # OPEN, and CLOSE_ERROR (a door that failed to close is not closed)


def hvac_active(power: Optional[int]) -> Optional[bool]:
    """None when unknown, else whether climate is doing anything."""
    if power is None or power == 0:
        return None
    return power in _HVAC_ACTIVE


def position_type(power_state: Optional[int]) -> Position.PositionType:
    """Driving or parking, from power state."""
    return Position.PositionType.DRIVING if power_state == POWER_DRIVE else Position.PositionType.PARKING


def speed_kmh(speed_mps: Optional[float]) -> Optional[float]:
    """Lucid reports chassis.speed in metres per second (proto comment); CarConnectivity wants km/h."""
    v = _f(speed_mps)
    return None if v is None else v * MPS_TO_KMH


DRIVING_STATES = (GenericVehicle.State.DRIVING, GenericVehicle.State.IGNITION_ON)


def poll_interval(states: Iterable[Optional[GenericVehicle.State]], idle_s: float, driving_s: float) -> float:
    """How long to wait before the next poll.

    A minute is plenty for a car asleep on a driveway and far too coarse for one moving:
    at 60 s a drive is a handful of points and a straight line between them, and speed
    read off consecutive fixes is an average over a mile. Poll faster only while a car is
    actually going somewhere, which is a small share of any day.
    """
    return driving_s if any(s in DRIVING_STATES for s in states) else idle_s


DOORS = {
    "front_left": "front_left_door",
    "front_right": "front_right_door",
    "rear_left": "rear_left_door",
    "rear_right": "rear_right_door",
    "frunk": "front_cargo",
    "trunk": "rear_cargo",
}
