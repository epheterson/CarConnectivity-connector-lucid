"""Lucid protobuf state -> CarConnectivity vocabulary. Pure functions, no I/O.

Unit bugs in this layer are invisible until someone eyeballs a dashboard, so each
conversion has a test. Up to 0.3.x they are pinned to values observed on a real
Gravity. The 0.4.0 reads come from the proto's field and enum names: one read-only
reading of a parked Gravity (2026-10-14) fixed their zero and sentinel cases, and
nothing has checked them while driving or charging. Lucid reports km, km/h for speed
(whatever the proto comment's "in meters/second" says; see speed_kmh), Celsius and bar.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable, Optional, Tuple

from carconnectivity.charging import Charging, ChargingConnector
from carconnectivity.doors import Doors
from carconnectivity.lights import Lights
from carconnectivity.windows import Windows
from carconnectivity.position import Position
from carconnectivity.vehicle import GenericVehicle

from carconnectivity_connectors.lucid.vehicle import AlarmCommand, AlarmStatus, ClimateLevel, LivingObjectDetection, SeatClimateCommand, \
    SentryInactiveReason, UnattendedOccupantDetection

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


# WindowPositionStatus: a fifteen-value ordinal of named positions rather than a
# percentage. Everything between fully closed and fully open is "ajar" here, including
# the two motor positions (ATP_REVERSE, the anti-trap back-off, and ANTICLATTER, the
# slight drop that stops a door slam rattling the glass) — a window in either is not
# closed, and that is what an alert cares about. See tillsteinbach/CarConnectivity#172
# for the discussion of carrying the named position alongside; this maps only what the
# model has a slot for.
WINDOW_CLOSED = {1, 9}      # FULLY_CLOSED, HARD_STOP_UP
WINDOW_OPEN = {5, 10}       # FULLY_OPEN, HARD_STOP_DOWN
WINDOW_UNKNOWN = {0, 6}     # UNKNOWN, UNKNOWN_DE_INITIALIZED


def window_open_state(position: Optional[int]) -> Windows.OpenState:
    """Lucid WindowPositionStatus int -> Windows.OpenState."""
    if position is None or position in WINDOW_UNKNOWN:
        return Windows.OpenState.UNKNOWN
    if position in WINDOW_CLOSED:
        return Windows.OpenState.CLOSED
    if position in WINDOW_OPEN:
        return Windows.OpenState.OPEN
    return Windows.OpenState.AJAR


WINDOWS = {
    "front_left": "left_front",
    "front_right": "right_front",
    "rear_left": "left_rear",
    "rear_right": "right_rear",
}


def connector_connection_state(charge_state: Optional[int]) -> ChargingConnector.ChargingConnectorConnectionState:
    """Whether a cable is in, read off ChargeState rather than the charge-port door.

    BodyState.charge_port is a DoorState — the little door, not the cable — so it says
    nothing about whether the car is plugged in. ChargeState does: 1 is NOT_CONNECTED,
    0 is UNKNOWN like every other enum's zero here, and every other value, charging or
    complete or faulted or discharging, is a state a car can only be in with a cable
    attached. This is the attribute a plug-in reminder waits on."""
    if charge_state is None or charge_state == 0:
        return ChargingConnector.ChargingConnectorConnectionState.UNKNOWN
    if charge_state in CHARGE_NOT_CONNECTED:
        return ChargingConnector.ChargingConnectorConnectionState.DISCONNECTED
    return ChargingConnector.ChargingConnectorConnectionState.CONNECTED


def external_power(charge_state: Optional[int]) -> ChargingConnector.ExternalPower:
    """ACTIVE while drawing, AVAILABLE when plugged in and not, UNAVAILABLE when there is
    no cable, UNKNOWN when the car has not said."""
    connection = connector_connection_state(charge_state)
    if connection is ChargingConnector.ChargingConnectorConnectionState.UNKNOWN:
        return ChargingConnector.ExternalPower.UNKNOWN
    if connection is ChargingConnector.ChargingConnectorConnectionState.DISCONNECTED:
        return ChargingConnector.ExternalPower.UNAVAILABLE
    return ChargingConnector.ExternalPower.ACTIVE if charge_state in CHARGE_CHARGING else ChargingConnector.ExternalPower.AVAILABLE


def nonzero(value: Any) -> Optional[float]:
    """A number the car reports, or None for 0: proto3 sends 0 for a field it left out,
    so on a reading that is never really zero, 0 is the car not saying."""
    number = _f(value)
    return number if number else None


def charge_session_energy(charge_state: Optional[int], kwh: Any) -> Optional[float]:
    """Energy (kWh) the car's own meter has added this session.

    Only while it is charging with something on the meter. A parked Gravity, unplugged,
    sends 0.0 (read 2026-10-14), and so does a car that leaves the field out, so 0 is never
    a reading here, and nor is anything outside a charge. charge_session_mi is not read:
    whether it is miles, as named, or km, as Lucid's other range figures are, is unverified."""
    if charge_state not in CHARGE_CHARGING:
        return None
    return nonzero(kwh)


# A minutes count of 65535 (0xFFFF) is "no estimate": a parked, unplugged Gravity sends
# session_minutes_remaining = 65535 (read 2026-10-14). Anything at or above it is the
# same sentinel or garbage.
NO_ESTIMATE_MINUTES = 65535
# Longer than any charge to a limit, even 120 V into a Gravity.
MAX_CHARGE_MINUTES = 7 * 24 * 60


def minutes_count(value: Any) -> Optional[float]:
    """A minutes field as a count, or None for 0 (not said), a negative, or the 65535 sentinel."""
    count = nonzero(value)
    if count is None or count < 0 or count >= NO_ESTIMATE_MINUTES:
        return None
    return count


def charge_finish(measured: datetime, charge_state: Optional[int], minutes: Any) -> Optional[datetime]:
    """When the charge reaches its limit: the car's minutes remaining from when it measured
    them. None unless it is charging and the count is a real one under a week."""
    left = minutes_count(minutes)
    if charge_state not in CHARGE_CHARGING or left is None or left > MAX_CHARGE_MINUTES:
        return None
    return measured + timedelta(minutes=left)


def cable_lock(state: Optional[int], charge_state: Optional[int]) -> Optional[ChargingConnector.ChargingConnectorLockState]:
    """ChargingState.cable_lock, a LockState: whether the cable is latched in the port. None
    unless a cable is in, since a latch with nothing in it is not a locked connector."""
    if connector_connection_state(charge_state) is not ChargingConnector.ChargingConnectorConnectionState.CONNECTED:
        return None
    if state == LOCK_LOCKED:
        return ChargingConnector.ChargingConnectorLockState.LOCKED
    if state == LOCK_UNLOCKED:
        return ChargingConnector.ChargingConnectorLockState.UNLOCKED
    return ChargingConnector.ChargingConnectorLockState.UNKNOWN


# SentryEnablementState: 0 UNKNOWN, 1 ENABLED, 2 DISABLED, 3 IDLE.
SENTRY_ENABLED, SENTRY_DISABLED, SENTRY_IDLE = 1, 2, 3


def sentry_armed(enablement: Optional[int]) -> Optional[bool]:
    """Whether Sentry is watching: ENABLED or IDLE.

    IDLE is what a parked, locked car reports while its security is on and nothing is
    happening — confirmed 2026-09-17 against a car the owner said was secured at the
    time (Eric: "TARS is halo secure"). The first version took ENABLED alone, on the
    grounds that under-counting Sentry was the recoverable mistake; with the reading
    confirmed, that would have priced a night of Sentry as a night of nothing. DISABLED
    is off; 0 is the car not having said."""
    if enablement is None or enablement == 0:
        return None
    return enablement in (SENTRY_ENABLED, SENTRY_IDLE)


# AlarmMode: 0 UNKNOWN, 1 OFF, 2 ON, 3 SILENT (the app's "push notifications only").
_ALARM_MODES = {1: AlarmCommand.Command.OFF, 2: AlarmCommand.Command.ON, 3: AlarmCommand.Command.SILENT}


def alarm_mode(mode: Optional[int]) -> Optional[AlarmCommand.Command]:
    """The security alarm's mode in the words the alarm command takes; None when unknown."""
    return _ALARM_MODES.get(mode) if mode is not None else None


# AlarmStatus: 0 UNKNOWN, 1 DISARMED, 2 ARMED, 3 PRE_ALARM, 4 TILT, 5 SHOCK, 6 INTRUSION, 7 PANIC_MODE.
ALARM_STATUS = dict(enumerate(AlarmStatus, start=1))
# SentryUsbDriveStatus: 0 UNKNOWN, 1 IDLE, 2 CONNECTED, 3 NOT_CONNECTED. What IDLE says about
# a drive is unverified, so it reads as unknown.
USB_CONNECTED, USB_NOT_CONNECTED = {2}, {3}
# LivingObjectDetectionStatus: 0 UNKNOWN, 1 DISABLED, 2 NOT_ACTIVE, 3..5 LEVEL_1..3_WARNING.
LIVING_OBJECT_DETECTION = dict(enumerate(LivingObjectDetection, start=1))
# UnattendedOccupantDetectionStatus: 0 UNKNOWN, 1 NO_REQUEST, 2..4 STAGE_1..3, 5 FAULT.
UNATTENDED_OCCUPANT_DETECTION = dict(enumerate(UnattendedOccupantDetection, start=1))
# KeyfobBatteryStatus: 0 UNKNOWN, 1 LOW, 2 SUFFICIENT.
KEYFOB_LOW, KEYFOB_OK = {1}, {2}

# SentryActiveState's flags, in proto order, each with the reason it names.
SENTRY_INACTIVE_REASONS = (
    ("inactive_reason_low_soc", SentryInactiveReason.LOW_BATTERY),
    ("inactive_reason_ota", SentryInactiveReason.UPDATING),
    ("inactive_reason_veh_occupied", SentryInactiveReason.OCCUPIED),
    ("inactive_reason_at_home", SentryInactiveReason.AT_HOME),
    ("inactive_reason_at_work", SentryInactiveReason.AT_WORK),
    ("inactive_reason_usb_full", SentryInactiveReason.USB_FULL),
    ("inactive_reason_usb_error", SentryInactiveReason.USB_ERROR),
    ("inactive_reason_tow_mode", SentryInactiveReason.TOW_MODE),
    ("inactive_reason_camera_error", SentryInactiveReason.CAMERA_ERROR),
    ("inactive_reason_uod", SentryInactiveReason.UNATTENDED_OCCUPANT),
    ("inactive_reason_creature_cmft_mode", SentryInactiveReason.CREATURE_COMFORT),
)


def pick(table: dict, value: Optional[int]) -> Any:
    """A Lucid enum int looked up in one of the tables above; None for 0 or anything unlisted."""
    return table.get(value) if value is not None else None


def sentry_inactive_reason(sentry_state: Any) -> Optional[SentryInactiveReason]:
    """Why Sentry is not recording: the first inactive_reason_* flag the car sets, in proto
    order; None when none is set. is_active itself is not read: a real Gravity, armed and
    idle, sent is_active false with every reason false (2026-10-14), so false does not mean
    "not recording", and an empty or absent block says nothing either."""
    active = _dig(sentry_state, "sentry_active_state")
    if active is None or getattr(active, "is_active", False):
        return None
    return next((why for flag, why in SENTRY_INACTIVE_REASONS if getattr(active, flag, False)), None)


# chassis.headlights is typed LightAction: 0 UNKNOWN, 1 FLASH, 2 ON, 3 OFF, 4 HAZARD_ON,
# 5 HAZARD_OFF. Only ON and OFF say whether the headlights are on.
LIGHT_ON, LIGHT_OFF = 2, 3


def headlights(action: Optional[int]) -> Optional[Lights.LightState]:
    """Whether the headlights are on; None when the car has not said, UNKNOWN for a flash or hazard."""
    if action is None or action == 0:
        return None
    return {LIGHT_ON: Lights.LightState.ON, LIGHT_OFF: Lights.LightState.OFF}.get(action, Lights.LightState.UNKNOWN)


def two_state(value: Optional[int], on: Iterable[int], off: Iterable[int]) -> Optional[bool]:
    """A Lucid enum read as on or off: True in `on`, False in `off`, None for 0 (the car
    not saying) or a value neither set names."""
    if value in on:
        return True
    if value in off:
        return False
    return None


# DefrostState: 0 UNKNOWN, 1 ON, 2 OFF. RearWindowHeatingStatus: 0 UNKNOWN, 1 OFF, 2 ON,
# 3 OFF and 4 ON "lost comm with DCM": the car has lost touch with the module that reports
# it, so those two are a stale reading and read unknown. The defrost command toggles the
# first, so it is the front entry of window_heatings.
DEFROST_ON, DEFROST_OFF = {1}, {2}
REAR_WINDOW_HEAT_ON, REAR_WINDOW_HEAT_OFF = {2}, {1}

# BatteryPreconStatus: 0 UNKNOWN, 1 OFF, 2 ON, 3 UNAVAILABLE (which is not on).
PRECON_ON, PRECON_OFF = {2}, {1, 3}
# KeylessDrivingState: 1 on, 2 off. The same numbers go out in KeylessDrivingActionRequest.
KEYLESS_ON, KEYLESS_OFF = {1}, {2}
DRIVE_MODE_VALET = 4


def valet(drive_mode: Optional[int]) -> Optional[bool]:
    """Whether the car is in Valet mode, read off DriveMode; 0 is the car not saying."""
    return None if not drive_mode else drive_mode == DRIVE_MODE_VALET


# MaxACState: 0 UNKNOWN, 1 OFF, 2 ON.
MAX_AC_ON, MAX_AC_OFF = {2}, {1}
# KeepClimateStatus: 0 UNKNOWN, 1 INACTIVE, 2 ENABLED, 3 CANCELED, 4 PET_MODE_ON. Creature
# comfort is the pet mode; keep-climate without it is not. CreatureComfortMode, which the
# command sends, is 1 OFF, 2 ON.
CREATURE_COMFORT_ON, CREATURE_COMFORT_OFF = {4}, {1, 2, 3}
CREATURE_COMFORT_MODE = {True: 2, False: 1}

# SeatClimateMode: 0 UNKNOWN, 2 OFF, 3 LOW, 4 MEDIUM, 5 HIGH (there is no 1).
SEAT_MODE = {ClimateLevel.OFF: 2, ClimateLevel.LOW: 3, ClimateLevel.MEDIUM: 4, ClimateLevel.HIGH: 5}
_SEAT_LEVEL = {mode: level for level, mode in SEAT_MODE.items()}
# SteeringWheelHeaterLevel: 0 UNKNOWN, 1 OFF, 2 LEVEL_1, 3 LEVEL_2, 4 LEVEL_3.
STEERING_LEVEL = {ClimateLevel.OFF: 1, ClimateLevel.LOW: 2, ClimateLevel.MEDIUM: 3, ClimateLevel.HIGH: 4}
_STEERING_CLIMATE_LEVEL = {level: climate for climate, level in STEERING_LEVEL.items()}

# The request and HvacState.seats share these field names. The car's heat zones come in
# bound pairs (the library notes zone 2 and 4 cannot be set apart on an Air), so the
# command sets a whole seat at once, as the app does.
_SEAT = SeatClimateCommand.Seat
_HEAT, _VENT = SeatClimateCommand.Mode.HEAT, SeatClimateCommand.Mode.VENT
SEAT_ZONES = {
    (_SEAT.DRIVER, _HEAT): ("driver_heat_backrest_zone1", "driver_heat_backrest_zone3", "driver_heat_cushion_zone2", "driver_heat_cushion_zone4"),
    (_SEAT.DRIVER, _VENT): ("driver_vent_backrest", "driver_vent_cushion"),
    (_SEAT.FRONT_PASSENGER, _HEAT): ("front_passenger_heat_backrest_zone1", "front_passenger_heat_backrest_zone3",
                                     "front_passenger_heat_cushion_zone2", "front_passenger_heat_cushion_zone4"),
    (_SEAT.FRONT_PASSENGER, _VENT): ("front_passenger_vent_backrest", "front_passenger_vent_cushion"),
    (_SEAT.REAR_LEFT, _HEAT): ("rear_passenger_heat_left",),
    (_SEAT.REAR_CENTER, _HEAT): ("rear_passenger_heat_center",),
    (_SEAT.REAR_RIGHT, _HEAT): ("rear_passenger_heat_right",),
}


# VehicleConfig's capability flags. Each *Availability enum is 0 UNKNOWN, 1 UNAVAILABLE,
# 2 AVAILABLE; only an explicit UNAVAILABLE refuses a command, so a car that does not
# say keeps all of them, as before. RearSeatConfig 2 is six seats: two second-row
# captain's chairs and no centre seat. StrutType 1 is a gas strut, which a remote
# command cannot pull shut.
FEATURE_UNAVAILABLE = 1
REAR_SEATS_SIX = 2
STRUT_GAS = 1


def seat_refusal(config: Any, seat: SeatClimateCommand.Seat, mode: SeatClimateCommand.Mode) -> Optional[str]:
    """Why this car cannot heat or ventilate this seat, by its own config; None when it can
    or has not said."""
    front = seat in (_SEAT.DRIVER, _SEAT.FRONT_PASSENGER)
    if front and mode is _VENT and _dig(config, "front_seats_ventilation") == FEATURE_UNAVAILABLE:
        return "this car's front seats are not ventilated"
    if front and mode is _HEAT and _dig(config, "front_seats_heating") == FEATURE_UNAVAILABLE:
        return "this car's front seats are not heated"
    if not front and _dig(config, "second_row_heated_seats") == FEATURE_UNAVAILABLE:
        return "this car's rear seats are not heated"
    if seat is _SEAT.REAR_CENTER and _dig(config, "rear_seat_config") == REAR_SEATS_SIX:
        return "this car has no rear centre seat"
    return None


def steering_heater_refusal(config: Any) -> Optional[str]:
    """Why this car cannot heat its steering wheel; None when it can or has not said."""
    if _dig(config, "heated_steering_wheel") == FEATURE_UNAVAILABLE:
        return "this car's steering wheel is not heated"
    return None


def frunk_close_refusal(config: Any) -> Optional[str]:
    """Why the frunk cannot be closed remotely: a gas strut closes by hand."""
    if _dig(config, "frunk_strut") == STRUT_GAS:
        return "this car's frunk has a gas strut and closes by hand"
    return None


def seat_attribute(seat: SeatClimateCommand.Seat, mode: SeatClimateCommand.Mode) -> str:
    """The published attribute for one seat's heat or ventilation: seat_heat_driver and so on."""
    return f"seat_{mode.value}_{seat.value.replace('-', '_')}"


def seat_level(modes: Iterable[Optional[int]]) -> Optional[ClimateLevel]:
    """One seat's level from its zones: the warmest (or most ventilated) one, since that is
    what someone sitting in it feels. None when no zone has said."""
    levels = [_SEAT_LEVEL[mode] for mode in modes if mode in _SEAT_LEVEL]
    if not levels:
        return None
    order = list(SEAT_MODE)
    return max(levels, key=order.index)


def steering_wheel_level(level: Optional[int], status: Optional[int] = None) -> Optional[ClimateLevel]:
    """The steering wheel heater's level; None when unknown. The level is a setting the car
    keeps while the heater is off, so a status of off (SteeringHeaterStatus 1) reads off."""
    if status == 1:
        return ClimateLevel.OFF
    return _STEERING_CLIMATE_LEVEL.get(level) if level is not None else None


# ChassisState carries four pressures in bar and, per wheel, a hard and a soft warning
# (WarningState: 0 unknown, 1 off, 2 on). There is no target pressure anywhere in the
# proto, so "low" is the car's own judgement, not a threshold applied here.
WARNING_ON, WARNING_OFF = 2, 1
TIRES = {
    "front_left": "front_left",
    "front_right": "front_right",
    "rear_left": "rear_left",
    "rear_right": "rear_right",
}


def tire_warning(flags: Iterable[Optional[int]]) -> Optional[bool]:
    """Any tire the car is warning about. True if any flag is on, False if every flag
    that reported is off, None if none reported. A car that has not said is not a car
    with a flat."""
    seen = False
    for flag in flags:
        if flag == WARNING_ON:
            return True
        if flag == WARNING_OFF:
            seen = True
    return False if seen else None


# SentryThreat: 0 UNKNOWN, 1 IDLE, 2 LEVEL_ONE, 3 LEVEL_TWO, 4 LEVEL_THREE, 5 NO_THREAT.
THREAT_LEVELS = {2, 3, 4}
THREAT_NONE = {1, 5}


def sentry_threat(level: Optional[int]) -> Optional[bool]:
    """Whether Sentry is currently reacting to something, at any level."""
    if level is None or level == 0:
        return None
    return level in THREAT_LEVELS


def warning_flag(state: Optional[int]) -> Optional[bool]:
    """WarningState int -> True on, False off, None not said."""
    if state == WARNING_ON:
        return True
    if state == WARNING_OFF:
        return False
    return None


# UpdateAvailability: 0 UNKNOWN, 1 UPDATE_AVAILABLE. There is no "no update" value, and
# proto3 delivers 0 for a field the car left out, so 0 says nothing either way.
UPDATE_AVAILABLE = 1


def update_available(value: Optional[int]) -> Optional[bool]:
    """True when the car says an update is waiting, None otherwise. Reading 0 as False
    published "no update" for a car that had simply not said."""
    return True if value == UPDATE_AVAILABLE else None


def text(value: Any) -> Optional[str]:
    """A proto string, or None for the empty string proto3 sends when it is left out."""
    return (value.strip() or None) if isinstance(value, str) else None


def waiting_update(software_update: Any) -> Optional[str]:
    """The version waiting to install, only when the car also says an update is AVAILABLE:
    whether version_available is cleared after an install, or names the version just
    installed, is unverified, so on its own it is not "waiting"."""
    if not update_available(_dig(software_update, "update_available")):
        return None
    return text(_dig(software_update, "version_available"))


def install_time(minutes: Any) -> Optional[timedelta]:
    """SoftwareUpdate.install_duration_minutes; None for 0 (the car not saying) or the 65535 sentinel."""
    left = minutes_count(minutes)
    return timedelta(minutes=left) if left is not None else None


# Model: 0 UNKNOWN, 1 AIR, 2 GRAVITY.
MODEL_AIR = 1


def release_notes_apply(model: Optional[int]) -> bool:
    """Whether the library's release notes are this car's. get_update_release_notes asks
    for DOCUMENT_TYPE_RELEASE_NOTES_POST, the Air's; a Gravity's are another document type
    (GTY_RELEASE_NOTES_POST) the library cannot ask for, and an Air's notes on a Gravity
    would describe the wrong car. So only a car that says it is an Air gets them."""
    return model == MODEL_AIR


def release_notes(document: Any) -> Tuple[Optional[str], Optional[str]]:
    """(text, link) from a GetDocumentInfoResponse; either is None when the reply is empty."""
    return text(_dig(document, "info", "description")), text(_dig(document, "url"))


def hvac_active(power: Optional[int]) -> Optional[bool]:
    """None when unknown, else whether climate is doing anything."""
    if power is None or power == 0:
        return None
    return power in _HVAC_ACTIVE


def position_type(power_state: Optional[int]) -> Position.PositionType:
    """Driving or parking, from power state."""
    return Position.PositionType.DRIVING if power_state == POWER_DRIVE else Position.PositionType.PARKING


def has_fix(lat: Optional[float], lon: Optional[float]) -> bool:
    """Whether a latitude and longitude are a position. A Gps block the car left out
    arrives as proto3's 0.0, 0.0, a point in the Gulf of Guinea no car parks at."""
    return lat is not None and lon is not None and (lat, lon) != (0.0, 0.0)


def speed_kmh(speed: Optional[float]) -> Optional[float]:
    """chassis.speed, which is already km/h.

    The proto annotates it "in meters/second" and the first version of this believed it,
    multiplying by 3.6. The car said otherwise: a 1.55-mile drive through a neighbourhood
    came out at 109, 171, 181 and 152 mph, every moving fix, not an occasional glitch.
    Read as km/h the same fixes are 30, 47, 50 and 42 — a plausible drive. The bridge
    that logged this car for a month before the connector did read it as km/h too, and
    its maxima matched the Teslas' on the same roads. The proto is a reverse-engineered
    annotation, not Lucid's word; the car's own numbers are.
    """
    return _f(speed)


DRIVING_STATES = (GenericVehicle.State.DRIVING, GenericVehicle.State.IGNITION_ON)


def poll_interval(states: Iterable[Optional[GenericVehicle.State]], idle_s: float, driving_s: float) -> float:
    """How long to wait before the next poll.

    A minute is plenty for a car asleep on a driveway and far too coarse for one moving:
    at 60 s a drive is a handful of points and a straight line between them, and speed
    read off consecutive fixes is an average over a mile. Poll faster only while a car is
    actually going somewhere, which is a small share of any day.
    """
    return driving_s if any(s in DRIVING_STATES for s in states) else idle_s


# What to do when Lucid refuses. It says only RESOURCE_EXHAUSTED, with an empty body:
# no quota, no retry-after, nothing to read. So the wait is a guess, and the first guess
# was a flat fifteen minutes, which is far too blunt. Every refusal observed on this
# account arrived one to six minutes into a drive, and a fifteen-minute silence then
# costs the whole drive and most of the parking afterwards — a 1 m 20 s trip on
# 2026-09-12 cost a quarter of an hour of blindness.
#
# Nobody knows how long it lasts, and it may well clear at once, so the first wait is
# five seconds and the ladder climbs through seconds before it reaches a minute:
# 5, 10, 20, 40, 80, 160, 320, 640, then the old 900 and no further. It resets the
# moment a poll succeeds.
#
# Guessing short costs one wasted call if the account is still short. Guessing long
# costs the drive. Those are not comparable, which is the whole argument for starting
# small — and it only became a safe bet once this connector was the sole client on the
# account, because retrying quickly alongside a second logger would just have spent
# more of the quota that ran out.
RATE_LIMIT_FIRST_BACKOFF_S = 5.0
RATE_LIMIT_MAX_BACKOFF_S = 900.0


def rate_limit_backoff(consecutive: int, first: float = RATE_LIMIT_FIRST_BACKOFF_S, cap: float = RATE_LIMIT_MAX_BACKOFF_S) -> float:
    """How long to wait after being refused, given how many refusals came in a row."""
    if consecutive < 1:
        return 0.0
    return min(first * (2 ** (consecutive - 1)), cap)


DOORS = {
    "front_left": "front_left_door",
    "front_right": "front_right_door",
    "rear_left": "rear_left_door",
    "rear_right": "rear_right_door",
    "frunk": "front_cargo",
    "trunk": "rear_cargo",
}
