"""CarConnectivity connector for Lucid Motors vehicles.

Writing into the model goes through GenericAttribute._set_value(), which is the
CarConnectivity convention for connectors; hence the module-wide disable.

Read-only. Polls the same mobile gRPC API the Lucid app uses, via
python-lucidmotors, and populates the CarConnectivity model. Reads never wake
the car (the library only wakes on commands with auto_wake=True, which this
never sets), so polling costs nothing in vampire drain.
"""
# pylint: disable=protected-access
from __future__ import annotations
from typing import TYPE_CHECKING

import logging
import threading
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

from carconnectivity.attributes import DurationAttribute, EnumAttribute
from carconnectivity.doors import Doors
from carconnectivity.drive import ElectricDrive, GenericDrive
from carconnectivity.enums import ConnectionState
from carconnectivity.errors import APICompatibilityError, AuthenticationError, RetrievalError, \
    TemporaryAuthenticationError, TooManyRequestsError
from carconnectivity.garage import Garage
from carconnectivity.units import Energy, Length, Power, Speed, Temperature
from carconnectivity.util import config_remove_credentials
from carconnectivity.vehicle import GenericVehicle

from carconnectivity_connectors.base.connector import BaseConnector
from carconnectivity_connectors.lucid import mapping
from carconnectivity_connectors.lucid.auth.session import LucidSession
from carconnectivity_connectors.lucid.vehicle import LucidElectricVehicle, LucidVehicle
from carconnectivity_connectors.lucid._version import __version__

if TYPE_CHECKING:
    from typing import Any, Dict, Optional
    from carconnectivity.carconnectivity import CarConnectivity

LOG: logging.Logger = logging.getLogger("carconnectivity.connectors.lucid")
LOG_API: logging.Logger = logging.getLogger("carconnectivity.connectors.lucid-api-debug")

_dig = mapping._dig  # pylint: disable=protected-access
_f = mapping._f  # pylint: disable=protected-access


class Connector(BaseConnector):  # pylint: disable=too-many-instance-attributes
    """Connector class for Lucid vehicles."""

    def __init__(self, connector_id: str, car_connectivity: CarConnectivity, config: Dict, *args, initialization: Optional[Dict] = None,
                 **kwargs) -> None:
        BaseConnector.__init__(self, connector_id=connector_id, car_connectivity=car_connectivity, config=config, log=LOG, api_log=LOG_API,
                               *args, initialization=initialization, **kwargs)

        self._background_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        self.connection_state: EnumAttribute[ConnectionState] = EnumAttribute(name="connection_state", parent=self, value_type=ConnectionState,
                                                                              value=ConnectionState.DISCONNECTED, tags={'connector_custom'})
        self.interval: DurationAttribute = DurationAttribute(name="interval", parent=self, tags={'connector_custom'})
        self.interval.minimum = timedelta(seconds=60)
        self.interval._is_changeable = True  # pylint: disable=protected-access

        LOG.info("Loading lucid connector with config %s", config_remove_credentials(config))

        if 'refresh_token_file' not in config or config['refresh_token_file'] is None:
            raise AuthenticationError("refresh_token_file was not found in config -- mint one with the lucidmotors login example and point at it")
        self.active_config['refresh_token_file'] = str(config['refresh_token_file'])
        if not Path(self.active_config['refresh_token_file']).expanduser().exists():
            # An absent credential is an authentication problem, and CarConnectivity's CI convention
            # expects AuthenticationError from a connector started without one.
            raise AuthenticationError(f"refresh_token_file does not exist: {self.active_config['refresh_token_file']}")

        self.active_config['interval'] = 60
        if 'interval' in config:
            self.active_config['interval'] = int(config['interval'])
            if self.active_config['interval'] < 60:
                raise ValueError('Interval must be at least 60 seconds')
        self.interval._set_value(timedelta(seconds=self.active_config['interval']))  # pylint: disable=protected-access

        self._session = LucidSession(Path(self.active_config['refresh_token_file']).expanduser())

    # -- lifecycle -------------------------------------------------------------
    def startup(self) -> None:
        self._background_thread = threading.Thread(target=self._background_loop, daemon=False)
        self._background_thread.name = 'carconnectivity.connectors.lucid-background'
        self._background_thread.start()
        self.healthy._set_value(value=True)  # pylint: disable=protected-access

    def _background_loop(self) -> None:  # pylint: disable=too-many-branches
        self._stop_event.clear()
        self._session.start()  # the asyncio loop belongs to this thread
        fetch: bool = True
        self.connection_state._set_value(value=ConnectionState.CONNECTING)  # pylint: disable=protected-access
        while not self._stop_event.is_set():
            interval: float = self.active_config['interval']
            try:
                try:
                    if fetch:
                        self.fetch_all()
                        fetch = False
                    else:
                        self.update_vehicles()
                    self.last_update._set_value(value=datetime.now(tz=timezone.utc))  # pylint: disable=protected-access
                    if self.interval.value is not None:
                        interval = self.interval.value.total_seconds()
                except Exception:
                    self.connection_state._set_value(value=ConnectionState.ERROR)  # pylint: disable=protected-access
                    if self.interval.value is not None:
                        interval = self.interval.value.total_seconds()
                    raise
            except TooManyRequestsError as err:
                LOG.error('Lucid is rate-limiting this account (%s). Will try again after 15 minutes', str(err))
                self._stop_event.wait(900)
            except (RetrievalError, APICompatibilityError, TemporaryAuthenticationError) as err:
                LOG.error('Error during update (%s). Will try again after %ss', str(err), interval)
                self._stop_event.wait(interval)
            except Exception as err:
                LOG.critical('Critical error during update: %s', traceback.format_exc())
                self.healthy._set_value(value=False)  # pylint: disable=protected-access
                self.connection_state._set_value(value=ConnectionState.ERROR)  # pylint: disable=protected-access
                raise err
            else:
                self.connection_state._set_value(value=ConnectionState.CONNECTED)  # pylint: disable=protected-access
                self._stop_event.wait(interval)
        self._session.close()
        self.connection_state._set_value(value=ConnectionState.DISCONNECTED)  # pylint: disable=protected-access

    def shutdown(self) -> None:
        for vehicle in self.car_connectivity.garage.list_vehicles():
            if len(vehicle.managing_connectors) == 1 and self in vehicle.managing_connectors:
                self.car_connectivity.garage.remove_vehicle(vehicle.id)
                vehicle.enabled = False
        self._stop_event.set()
        if self._background_thread is not None:
            self._background_thread.join()
        BaseConnector.shutdown(self)

    # -- fetching --------------------------------------------------------------
    def fetch_all(self) -> None:
        """First fetch: vehicles and their full state."""
        self.fetch_vehicles()
        self.car_connectivity.transaction_end()

    def update_vehicles(self) -> None:
        """Subsequent polls."""
        # One gRPC call returns every vehicle with full state, so "update" and "fetch" are the same request.
        self.fetch_vehicles()

    def fetch_vehicles(self) -> None:
        """Fetch every vehicle on the account, add new ones to the garage, drop ones that disappeared, apply state."""
        garage: Garage = self.car_connectivity.garage
        seen: set[str] = set()
        for lucid_vehicle in self._session.fetch_vehicles():
            vin: Optional[str] = _dig(lucid_vehicle, "config", "vin")
            if not vin:
                raise APICompatibilityError('Lucid vehicle without a VIN')
            if vin in self.active_config['hide_vins']:
                LOG.warning('Vehicle %s is hidden in config', vin)
                continue
            seen.add(vin)
            vehicle: Optional[GenericVehicle] = garage.get_vehicle(vin)
            if vehicle is None:
                vehicle = LucidElectricVehicle(vin=vin, garage=garage, managing_connector=self, initialization=garage.get_initialization(vin))
                garage.add_vehicle(vin, vehicle)
            elif not isinstance(vehicle, LucidElectricVehicle):
                vehicle = LucidElectricVehicle(garage=garage, origin=vehicle)
                garage.replace_vehicle(vin, vehicle)
            self._apply(vehicle, lucid_vehicle)
        for vin in set(garage.list_vehicle_vins()) - seen:
            gone = garage.get_vehicle(vin)
            if gone is not None and gone.is_managed_by_connector(self):
                garage.remove_vehicle(vin)

    # -- mapping ---------------------------------------------------------------
    def _apply(self, vehicle: LucidVehicle, lv: Any) -> None:
        """Copy one Lucid vehicle (config + state) into the CarConnectivity model."""
        cfg, st = _dig(lv, "config"), _dig(lv, "state")
        ts_ms = _f(_dig(st, "last_updated_ms"))
        measured = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc) if ts_ms else datetime.now(tz=timezone.utc)
        self._apply_identity(vehicle, cfg, st, measured)
        self._apply_drive(vehicle, st, measured)
        if isinstance(vehicle, LucidElectricVehicle):
            self._apply_charging(vehicle, st, measured)
        self._apply_position(vehicle, st, measured)
        self._apply_doors(vehicle, st, measured)
        self._apply_climate(vehicle, st, measured)

    @staticmethod
    def _apply_identity(sv: LucidVehicle, cfg: Any, st: Any, measured: datetime) -> None:
        sv.type._set_value(GenericVehicle.Type.ELECTRIC)
        if _dig(cfg, "nickname"):
            sv.name._set_value(_dig(cfg, "nickname"))
        model = _dig(cfg, "model")
        if model is not None:
            sv.model._set_value({1: 'Air', 2: 'Gravity'}.get(int(model), f'Lucid model {model}'))
        # VehicleConfig carries no model year (checked against the proto 2026-09-03); model_year stays unset.
        sw = _dig(st, "chassis", "software_version")
        if sw and hasattr(sv, 'software') and hasattr(sv.software, 'version'):
            sv.software.version._set_value(sw, measured=measured)
        power, cs = _dig(st, "power"), _dig(st, "charging", "charge_state")
        sv.state._set_value(mapping.vehicle_state(power, cs), measured=measured)
        reachable = GenericVehicle.ConnectionState.REACHABLE if mapping.is_awake(power, cs) else GenericVehicle.ConnectionState.OFFLINE
        sv.connection_state._set_value(reachable, measured=measured)
        odo = _f(_dig(st, "chassis", "odometer_km"))
        if odo is not None:
            sv.odometer._set_value(odo, measured=measured, unit=Length.KM)
        sv.outside_temperature._set_value(mapping.plausible_temp(_f(_dig(st, "cabin", "exterior_temp"))), measured=measured, unit=Temperature.C)

    @staticmethod
    def _apply_drive(sv: LucidVehicle, st: Any, measured: datetime) -> None:
        drive_id = 'primary'
        drive: GenericDrive
        if drive_id in sv.drives.drives:
            drive = sv.drives.drives[drive_id]
        else:
            drive = ElectricDrive(drive_id=drive_id, drives=sv.drives, initialization=sv.drives.get_initialization(drive_id))
            drive.type._set_value(GenericDrive.Type.ELECTRIC)
            sv.drives.add_drive(drive)
        bat = _dig(st, "battery")
        drive.level._set_value(_f(_dig(bat, "charge_percent")), measured=measured)
        rng = _f(_dig(bat, "remaining_range"))
        drive.range._set_value(rng, measured=measured, unit=Length.KM)
        sv.drives.total_range._set_value(rng, measured=measured, unit=Length.KM)
        if isinstance(drive, ElectricDrive):
            # Lucid reports capacity_kwhr (the pack's usable capacity, a constant) and kwhr (energy
            # remaining right now, which tracks SoC). CarConnectivity's available_capacity is the
            # constant usable capacity; the database plugin stores it as drives.capacity and the
            # dashboards multiply SoC deltas by it. Remaining energy has no CarConnectivity field, so it
            # is not mapped. Gross (total) capacity is not reported by the car, so it is left unset.
            drive.battery.available_capacity._set_value(_f(_dig(bat, "capacity_kwhr")), measured=measured, unit=Energy.KWH)
            drive.battery.temperature_min._set_value(mapping.plausible_temp(_f(_dig(bat, "min_cell_temp"))), measured=measured, unit=Temperature.C)
            drive.battery.temperature_max._set_value(mapping.plausible_temp(_f(_dig(bat, "max_cell_temp"))), measured=measured, unit=Temperature.C)

    @staticmethod
    def _apply_charging(sv: LucidElectricVehicle, st: Any, measured: datetime) -> None:
        ch = _dig(st, "charging")
        cs = _dig(ch, "charge_state")
        charging = cs in mapping.CHARGE_CHARGING
        sv.charging.state._set_value(mapping.charging_state(cs), measured=measured)
        sv.charging.type._set_value(mapping.charging_type(_dig(ch, "energy_type"), cs), measured=measured)
        kw = _f(_dig(ch, "charge_rate_kwh_precise"))
        sv.charging.power._set_value(kw if charging else 0.0, measured=measured, unit=Power.KW)
        rate_mph = _f(_dig(ch, "charge_rate_mph_precise"))
        sv.charging.rate._set_value(rate_mph * 1.609344 if (rate_mph is not None and charging) else 0.0, measured=measured, unit=Speed.KMH)
        limit = _f(_dig(ch, "charge_limit_percent"))
        if limit is not None:
            sv.charging.settings.target_level._set_value(limit, measured=measured)

    @staticmethod
    def _apply_position(sv: LucidVehicle, st: Any, measured: datetime) -> None:
        gps = _dig(st, "gps")
        lat, lon = _f(_dig(gps, "location", "latitude")), _f(_dig(gps, "location", "longitude"))
        if lat is None or lon is None:
            return
        sv.position.latitude._set_value(lat, measured=measured)
        sv.position.longitude._set_value(lon, measured=measured)
        sv.position.heading._set_value(_f(_dig(gps, "heading_precise")), measured=measured)
        sv.position.position_type._set_value(mapping.position_type(_dig(st, "power")), measured=measured)

    @staticmethod
    def _apply_doors(sv: LucidVehicle, st: Any, measured: datetime) -> None:
        body = _dig(st, "body")
        sv.doors.lock_state._set_value(mapping.lock_state(_dig(body, "door_locks")), measured=measured)
        any_open = False
        reported = False
        for door_id, field in mapping.DOORS.items():
            raw = _dig(body, field)
            if raw is None:
                continue
            if door_id in sv.doors.doors:
                door = sv.doors.doors[door_id]
            else:
                door = Doors.Door(door_id=door_id, doors=sv.doors, initialization=sv.doors.get_initialization(door_id))
                sv.doors.doors[door_id] = door
            state = mapping.door_open_state(raw)
            door.open_state._set_value(state, measured=measured)
            door.lock_state._set_value(sv.doors.lock_state.value, measured=measured)
            any_open = any_open or state in (Doors.OpenState.OPEN, Doors.OpenState.AJAR)
            reported = True
        # Only summarise doors that actually reported. Without this a car whose body
        # block is absent publishes "all doors closed", which is a reassuring answer
        # invented out of no data at all.
        if reported:
            sv.doors.open_state._set_value(Doors.OpenState.OPEN if any_open else Doors.OpenState.CLOSED, measured=measured)
        else:
            sv.doors.open_state._set_value(Doors.OpenState.UNKNOWN, measured=measured)

    @staticmethod
    def _apply_climate(sv: LucidVehicle, st: Any, measured: datetime) -> None:
        hvac_on = mapping.hvac_active(_dig(st, "hvac", "power"))
        if hvac_on is None:
            clim_state = sv.climatization.ClimatizationState.UNKNOWN
        elif hvac_on:
            clim_state = sv.climatization.ClimatizationState.VENTILATION
        else:
            clim_state = sv.climatization.ClimatizationState.OFF
        sv.climatization.state._set_value(clim_state, measured=measured)
        target = _f(_dig(st, "hvac", "front_left_set_temperature"))
        if target is not None:
            sv.climatization.settings.target_temperature._set_value(target, measured=measured, unit=Temperature.C)

    # -- identity --------------------------------------------------------------
    def get_version(self) -> str:
        return __version__

    def get_type(self) -> str:
        return "carconnectivity-connector-lucid"

    def get_name(self) -> str:
        return "Lucid Connector"
