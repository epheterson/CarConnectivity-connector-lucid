"""Lucid vehicle classes. Every Lucid is electric, so the electric subclass is
what the connector always creates; the generic one exists for the promotion
pattern CarConnectivity uses and for parity with other connectors."""
from __future__ import annotations
from typing import TYPE_CHECKING

from enum import Enum

from carconnectivity.attributes import BooleanAttribute, FloatAttribute, SpeedAttribute, TemperatureAttribute
from carconnectivity.commands import GenericCommand
from carconnectivity.errors import SetterError
from carconnectivity.units import Speed, Temperature
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
    """Open or close one closure: the trunk, the frunk, the charge port door."""
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
