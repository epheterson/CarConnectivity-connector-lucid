"""Lucid vehicle classes. Every Lucid is electric, so the electric subclass is
what the connector always creates; the generic one exists for the promotion
pattern CarConnectivity uses and for parity with other connectors."""
from __future__ import annotations
from typing import TYPE_CHECKING

from carconnectivity.attributes import SpeedAttribute
from carconnectivity.units import Speed
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


class LucidElectricVehicle(ElectricVehicle, LucidVehicle):
    """A Lucid electric vehicle -- i.e. any Lucid."""
    def __init__(self, vin: Optional[str] = None, garage: Optional[Garage] = None, managing_connector: Optional[BaseConnector] = None,
                 origin: Optional[LucidVehicle] = None, initialization: Optional[Dict] = None) -> None:
        if origin is not None:
            super().__init__(garage=garage, origin=origin, initialization=initialization)
        else:
            super().__init__(vin=vin, garage=garage, managing_connector=managing_connector, initialization=initialization)
