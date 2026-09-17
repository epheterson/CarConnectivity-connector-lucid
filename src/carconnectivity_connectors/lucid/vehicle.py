"""Lucid vehicle classes. Every Lucid is electric, so the electric subclass is
what the connector always creates; the generic one exists for the promotion
pattern CarConnectivity uses and for parity with other connectors."""
from __future__ import annotations
from typing import TYPE_CHECKING

from carconnectivity.attributes import BooleanAttribute, FloatAttribute, SpeedAttribute, TemperatureAttribute
from carconnectivity.units import Speed, Temperature
from carconnectivity.vehicle import GenericVehicle, ElectricVehicle

if TYPE_CHECKING:
    from typing import Optional, Dict
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


class LucidElectricVehicle(ElectricVehicle, LucidVehicle):
    """A Lucid electric vehicle -- i.e. any Lucid."""
    def __init__(self, vin: Optional[str] = None, garage: Optional[Garage] = None, managing_connector: Optional[BaseConnector] = None,
                 origin: Optional[LucidVehicle] = None, initialization: Optional[Dict] = None) -> None:
        if origin is not None:
            super().__init__(garage=garage, origin=origin, initialization=initialization)
        else:
            super().__init__(vin=vin, garage=garage, managing_connector=managing_connector, initialization=initialization)
