"""Lucid vehicle classes. Every Lucid is electric, so the electric subclass is
what the connector always creates; the generic one exists for the promotion
pattern CarConnectivity uses and for parity with other connectors."""
from __future__ import annotations
from typing import TYPE_CHECKING

from enum import Enum

from carconnectivity.attributes import BooleanAttribute, DurationAttribute, EnergyAttribute, EnumAttribute, FloatAttribute, SpeedAttribute, \
    StringAttribute, TemperatureAttribute
from carconnectivity.commands import GenericCommand
from carconnectivity.doors import Doors
from carconnectivity.errors import SetterError
from carconnectivity.units import Energy, Speed, Temperature
from carconnectivity.vehicle import GenericVehicle, ElectricVehicle

if TYPE_CHECKING:
    from typing import Optional, Dict, Union
    from carconnectivity.objects import GenericObject
    from carconnectivity.garage import Garage
    from carconnectivity_connectors.base.connector import BaseConnector


class LucidVehicle(GenericVehicle):  # pylint: disable=too-many-instance-attributes
    """A Lucid vehicle."""
    def __init__(self, vin: Optional[str] = None, garage: Optional[Garage] = None, managing_connector: Optional[BaseConnector] = None,
                 origin: Optional[LucidVehicle] = None, initialization: Optional[Dict] = None) -> None:
        if origin is not None:
            super().__init__(garage=garage, origin=origin, initialization=initialization)
        else:
            super().__init__(vin=vin, garage=garage, managing_connector=managing_connector, initialization=initialization)
        self.manufacturer._set_value(value='Lucid')  # pylint: disable=protected-access
        # The car reports how fast it is going and CarConnectivity's model has nowhere to
        # put it: neither Position nor GenericVehicle carries a speed. SpeedAttribute
        # exists though, so it hangs here as a connector-specific attribute rather than
        # being thrown away. Marked connector_custom, the same way `interval` is, so it
        # is clearly not part of the shared model.
        self.speed: SpeedAttribute = SpeedAttribute(name="speed", parent=self, unit=Speed.KMH, precision=0.1, minimum=0.0,
                                                    tags={'connector_custom'})
        # Two more the model has no slot for. The cabin temperature sits beside
        # outside_temperature, which the model does carry; Sentry is a Lucid feature
        # with no counterpart in the shared model, so it is a plain boolean here.
        self.inside_temperature: TemperatureAttribute = TemperatureAttribute(
            name="inside_temperature", parent=self, unit=Temperature.C, precision=0.1, tags={'connector_custom'})
        self.sentry: BooleanAttribute = BooleanAttribute(name="sentry", parent=self, tags={'connector_custom'})
        # Tire pressures, in bar as the car reports them: CarConnectivity has no pressure
        # unit yet (tillsteinbach/CarConnectivity#172 is where a TPMS model is being
        # discussed), so these are plain floats named for what they hold. The warning is
        # the car's own — the proto carries no target pressure, so no threshold is
        # applied here; a placard is per car, per tyre size, sometimes per axle, and
        # getting it wrong tells someone a low tire is fine.
        self.tire_pressure_front_left: FloatAttribute = FloatAttribute(
            name="tire_pressure_front_left", parent=self, precision=0.01, minimum=0.0, tags={'connector_custom'})
        self.tire_pressure_front_right: FloatAttribute = FloatAttribute(
            name="tire_pressure_front_right", parent=self, precision=0.01, minimum=0.0, tags={'connector_custom'})
        self.tire_pressure_rear_left: FloatAttribute = FloatAttribute(
            name="tire_pressure_rear_left", parent=self, precision=0.01, minimum=0.0, tags={'connector_custom'})
        self.tire_pressure_rear_right: FloatAttribute = FloatAttribute(
            name="tire_pressure_rear_right", parent=self, precision=0.01, minimum=0.0, tags={'connector_custom'})
        self.tire_warning: BooleanAttribute = BooleanAttribute(name="tire_warning", parent=self, tags={'connector_custom'})
        # Sentry's threat level as a boolean — something is happening to the car — and
        # the battery as the car describes it: pack capacity and energy in kWh, a health
        # warning flag, and whether a software update is waiting. All connector-specific.
        self.sentry_threat: BooleanAttribute = BooleanAttribute(name="sentry_threat", parent=self, tags={'connector_custom'})
        self.battery_capacity_kwh: FloatAttribute = FloatAttribute(
            name="battery_capacity_kwh", parent=self, precision=0.01, minimum=0.0, tags={'connector_custom'})
        self.battery_energy_kwh: FloatAttribute = FloatAttribute(name="battery_energy_kwh", parent=self, precision=0.01, minimum=0.0, tags={'connector_custom'})
        self.battery_health_warning: BooleanAttribute = BooleanAttribute(name="battery_health_warning", parent=self, tags={'connector_custom'})
        self.software_update_available: BooleanAttribute = BooleanAttribute(name="software_update_available", parent=self, tags={'connector_custom'})
        # The state behind the 0.3.0 commands, so a UI can show each one as a toggle.
        # Enhanced deterrence reads like `sentry`: enabled or idle is on. The alarm mode
        # is in the words the alarm command takes.
        self.enhanced_deterrence: BooleanAttribute = BooleanAttribute(name="enhanced_deterrence", parent=self, tags={'connector_custom'})
        self.alarm: EnumAttribute[AlarmCommand.Command] = EnumAttribute(name="alarm", parent=self, value_type=AlarmCommand.Command,
                                                                        tags={'connector_custom'})
        # The charge port's door, which the model has no slot for. Not one of `doors`: the
        # door stands open for every charge, and "doors open" would then mean "charging".
        self.charge_port: EnumAttribute[Doors.OpenState] = EnumAttribute(name="charge_port", parent=self, value_type=Doors.OpenState,
                                                                         tags={'connector_custom'})
        self.battery_preconditioning: BooleanAttribute = BooleanAttribute(name="battery_preconditioning", parent=self, tags={'connector_custom'})
        # Keyless driving: the car drives with no key present while it is on. Valet is
        # read off the drive mode; nothing in the API sets it.
        self.keyless_driving: BooleanAttribute = BooleanAttribute(name="keyless_driving", parent=self, tags={'connector_custom'})
        self.valet: BooleanAttribute = BooleanAttribute(name="valet", parent=self, tags={'connector_custom'})
        self.max_ac: BooleanAttribute = BooleanAttribute(name="max_ac", parent=self, tags={'connector_custom'})
        self.creature_comfort: BooleanAttribute = BooleanAttribute(name="creature_comfort", parent=self, tags={'connector_custom'})
        self.steering_wheel_heater: EnumAttribute[ClimateLevel] = EnumAttribute(name="steering_wheel_heater", parent=self, value_type=ClimateLevel,
                                                                                tags={'connector_custom'})
        # One level per seat and mode, named as mapping.seat_attribute names them: the
        # front seats heat and ventilate, the rear ones heat.
        self.seat_climate: Dict[str, EnumAttribute[ClimateLevel]] = {
            name: EnumAttribute(name=name, parent=self, value_type=ClimateLevel, tags={'connector_custom'})
            for name in ("seat_heat_driver", "seat_vent_driver", "seat_heat_front_passenger", "seat_vent_front_passenger",
                         "seat_heat_rear_left", "seat_heat_rear_center", "seat_heat_rear_right")}
        # 0.4.0. The energy the car's own meter says this charging session has added; set
        # only while charging. The model's Charging has no slot for it.
        self.charge_session_energy: EnergyAttribute = EnergyAttribute(name="charge_session_energy", parent=self, unit=Energy.KWH, precision=0.01,
                                                                      minimum=0.0, tags={'connector_custom'})
        # What the alarm and Sentry are actually doing, beside the modes above: the alarm going
        # off and why, why Sentry is paused when the car gives a reason, and whether it has a
        # drive to record to. Then the two occupant warnings and the key fob's battery.
        self.alarm_status: EnumAttribute[AlarmStatus] = EnumAttribute(name="alarm_status", parent=self, value_type=AlarmStatus, tags={'connector_custom'})
        self.sentry_inactive_reason: EnumAttribute[SentryInactiveReason] = EnumAttribute(name="sentry_inactive_reason", parent=self,
                                                                                         value_type=SentryInactiveReason, tags={'connector_custom'})
        self.sentry_usb_drive_connected: BooleanAttribute = BooleanAttribute(name="sentry_usb_drive_connected", parent=self, tags={'connector_custom'})
        self.living_object_detection: EnumAttribute[LivingObjectDetection] = EnumAttribute(name="living_object_detection", parent=self,
                                                                                           value_type=LivingObjectDetection, tags={'connector_custom'})
        self.unattended_occupant_detection: EnumAttribute[UnattendedOccupantDetection] = EnumAttribute(
            name="unattended_occupant_detection", parent=self, value_type=UnattendedOccupantDetection, tags={'connector_custom'})
        self.keyfob_battery_low: BooleanAttribute = BooleanAttribute(name="keyfob_battery_low", parent=self, tags={'connector_custom'})
        # Gps.elevation exactly as sent, with no unit: it is not metres, feet, decimetres or
        # centimetres, so it is not position.altitude. Published for calibration.
        self.gps_elevation_raw: FloatAttribute = FloatAttribute(name="gps_elevation_raw", parent=self, tags={'connector_custom'})
        # BatteryState.battery_health_level as the car sends it. Not named or scaled as a state
        # of health: the proto does not say what it measures, and no car has been read yet.
        # The update that is waiting: its version, how long it takes to install, and Lucid's
        # release notes for it (text and link), fetched once per version.
        self.software_update_version: StringAttribute = StringAttribute(name="software_update_version", parent=self, tags={'connector_custom'})
        self.software_update_install_time: DurationAttribute = DurationAttribute(name="software_update_install_time", parent=self,
                                                                                 tags={'connector_custom'})
        self.software_update_release_notes: StringAttribute = StringAttribute(name="software_update_release_notes", parent=self,
                                                                              tags={'connector_custom'})
        self.software_update_release_notes_url: StringAttribute = StringAttribute(name="software_update_release_notes_url", parent=self,
                                                                                  tags={'connector_custom'})
        # Every version the car has installed, oldest first, as JSON text:
        # [{"version": "3.1.51", "at": "2025-12-19T10:00:00Z"}, ...] with the time in UTC.
        # Read from Lucid's OTA history once, and again when the car's version changes.
        self.software_history: StringAttribute = StringAttribute(name="software_history", parent=self, tags={'connector_custom'})
        self.battery_health_level: FloatAttribute = FloatAttribute(name="battery_health_level", parent=self, precision=0.01, tags={'connector_custom'})


class LucidElectricVehicle(ElectricVehicle, LucidVehicle):
    """A Lucid electric vehicle -- i.e. any Lucid."""
    def __init__(self, vin: Optional[str] = None, garage: Optional[Garage] = None, managing_connector: Optional[BaseConnector] = None,
                 origin: Optional[LucidVehicle] = None, initialization: Optional[Dict] = None) -> None:
        if origin is not None:
            super().__init__(garage=garage, origin=origin, initialization=initialization)
        else:
            super().__init__(vin=vin, garage=garage, managing_connector=managing_connector, initialization=initialization)


class ClimateLevel(Enum):
    """A seat or steering-wheel heater level, and a seat's ventilation level: the four
    the Lucid app offers. The car's own numbers differ per heater; the connector maps them."""
    OFF = 'off'
    LOW = 'low'
    MEDIUM = 'medium'
    HIGH = 'high'

    def __str__(self) -> str:
        return self.value


class _Named(Enum):
    """A state the connector publishes in its own words, printed as its value."""
    def __str__(self) -> str:
        return self.value


class AlarmStatus(_Named):
    """What the security alarm is doing (AlarmStatus): armed and quiet, or going off and why."""
    DISARMED = 'disarmed'
    ARMED = 'armed'
    PRE_ALARM = 'pre-alarm'
    TILT = 'tilt'
    SHOCK = 'shock'
    INTRUSION = 'intrusion'
    PANIC = 'panic'


class SentryInactiveReason(_Named):
    """Why Sentry is not recording, from SentryActiveState's inactive_reason_* flags."""
    LOW_BATTERY = 'low-battery'
    UPDATING = 'updating'
    OCCUPIED = 'occupied'
    AT_HOME = 'at-home'
    AT_WORK = 'at-work'
    USB_FULL = 'usb-full'
    USB_ERROR = 'usb-error'
    TOW_MODE = 'tow-mode'
    CAMERA_ERROR = 'camera-error'
    UNATTENDED_OCCUPANT = 'unattended-occupant'
    CREATURE_COMFORT = 'creature-comfort'


class LivingObjectDetection(_Named):
    """BodyState.living_object_detection_status: a child or pet left in the car, by warning level."""
    DISABLED = 'disabled'
    NOT_ACTIVE = 'not-active'
    LEVEL_1 = 'level-1'
    LEVEL_2 = 'level-2'
    LEVEL_3 = 'level-3'


class UnattendedOccupantDetection(_Named):
    """BodyState.unattended_occupant_detection_status, by stage."""
    NO_REQUEST = 'no-request'
    STAGE_1 = 'stage-1'
    STAGE_2 = 'stage-2'
    STAGE_3 = 'stage-3'
    FAULT = 'fault'


class ChoiceCommand(GenericCommand):
    """A command of the connector's own that takes one word from a fixed set, its
    `Command` enum: the word, the enum member, or {"command": word}, like the standard
    commands. Anything else is a SetterError before the hook, so before the car, sees it.
    CarConnectivity has no standard command for these, hence the connector's own."""
    Command: type[Enum]

    def __init__(self, name: str, parent: Optional[GenericObject] = None, initialization: Optional[Dict] = None) -> None:
        super().__init__(name=name, parent=parent, initialization=initialization)

    @property
    def value(self) -> Optional[Union[str, Dict]]:
        return super().value

    @value.setter
    def value(self, new_value: Optional[Union[str, Dict]]) -> None:
        new_value = self._execute_on_set_hook(new_value, early_hook=True)
        raw = new_value.get('command') if isinstance(new_value, dict) else new_value
        if isinstance(raw, str):
            raw = raw.strip()
        try:
            command = self.Command(raw)
        except ValueError as err:
            raise SetterError(f'Invalid value for {self.name}: {raw!r}; use one of {[str(c) for c in self.Command]}') from err
        if not self._is_changeable:
            raise TypeError('You cannot use this command. Command is not implemented.')
        new_value = self._execute_on_set_hook({'command': command}, early_hook=False)
        self._set_value(new_value)


class OpenCloseCommand(ChoiceCommand):
    """Open or close one closure: the trunk or the frunk."""
    class Command(Enum):
        """Open or close."""
        OPEN = 'open'
        CLOSE = 'close'

        def __str__(self) -> str:
            return self.value


class OnOffCommand(ChoiceCommand):
    """Turn one feature on or off: Sentry, Max A/C, the headlights and the rest."""
    class Command(Enum):
        """On or off."""
        ON = 'on'
        OFF = 'off'

        def __str__(self) -> str:
            return self.value


class WindowsCommand(ChoiceCommand):
    """All four windows at once. The car offers "vent", which drops them a little (a
    second vent drops them further), not a full open, so that is the word here."""
    class Command(Enum):
        """Vent or close."""
        VENT = 'vent'
        CLOSE = 'close'

        def __str__(self) -> str:
            return self.value


class AlarmCommand(ChoiceCommand):
    """The security alarm: on, off, or silent (the app's "push notifications only")."""
    class Command(Enum):
        """Off, on or silent."""
        OFF = 'off'
        ON = 'on'
        SILENT = 'silent'

        def __str__(self) -> str:
            return self.value


class LevelCommand(ChoiceCommand):
    """One heater's level: off, low, medium, high. The steering wheel's."""
    Command = ClimateLevel


class SeatClimateCommand(GenericCommand):
    """One seat's heating or ventilation: "driver heat high", "front-passenger vent low",
    or {"seat": ..., "mode": ..., "level": ...}. The rear seats heat and do not ventilate,
    so a rear vent is refused here rather than sent."""
    def __init__(self, name: str, parent: Optional[GenericObject] = None, initialization: Optional[Dict] = None) -> None:
        super().__init__(name=name, parent=parent, initialization=initialization)

    @property
    def value(self) -> Optional[Union[str, Dict]]:
        return super().value

    @value.setter
    def value(self, new_value: Optional[Union[str, Dict]]) -> None:
        new_value = self._execute_on_set_hook(new_value, early_hook=True)
        if isinstance(new_value, str):
            words = new_value.split()
        elif isinstance(new_value, dict):
            words = [new_value.get('seat'), new_value.get('mode'), new_value.get('level')]
        else:
            words = []
        usage = (f'use "<seat> <mode> <level>", seat one of {[str(s) for s in SeatClimateCommand.Seat]}, '
                 f'mode heat or vent (front seats only), level one of {[str(c) for c in ClimateLevel]}')
        if len(words) != 3:
            raise SetterError(f'Invalid value for {self.name}: {new_value!r}; {usage}')
        try:
            seat, mode, level = SeatClimateCommand.Seat(words[0]), SeatClimateCommand.Mode(words[1]), ClimateLevel(words[2])
        except ValueError as err:
            raise SetterError(f'Invalid value for {self.name}: {new_value!r}; {usage}') from err
        if mode is SeatClimateCommand.Mode.VENT and seat not in (SeatClimateCommand.Seat.DRIVER, SeatClimateCommand.Seat.FRONT_PASSENGER):
            raise SetterError(f'Invalid value for {self.name}: the rear seats are heated, not ventilated; {usage}')
        if not self._is_changeable:
            raise TypeError('You cannot use this command. Command is not implemented.')
        new_value = self._execute_on_set_hook({'seat': seat, 'mode': mode, 'level': level}, early_hook=False)
        self._set_value(new_value)

    class Seat(Enum):
        """The five seats the car can heat."""
        DRIVER = 'driver'
        FRONT_PASSENGER = 'front-passenger'
        REAR_LEFT = 'rear-left'
        REAR_CENTER = 'rear-center'
        REAR_RIGHT = 'rear-right'

        def __str__(self) -> str:
            return self.value

    class Mode(Enum):
        """Heat or ventilate."""
        HEAT = 'heat'
        VENT = 'vent'

        def __str__(self) -> str:
            return self.value
