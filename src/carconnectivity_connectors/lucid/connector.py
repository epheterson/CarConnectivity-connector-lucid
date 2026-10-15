"""CarConnectivity connector for Lucid Motors vehicles.

Writing into the model goes through GenericAttribute._set_value(), which is the
CarConnectivity convention for connectors; hence the module-wide disable.

Polls the same mobile gRPC API the Lucid app uses, via python-lucidmotors, and
populates the CarConnectivity model. Reads never wake the car (the library only
wakes on commands with auto_wake=True, which this never sets), so polling costs
nothing in vampire drain.

Read-only unless the config says `"commands": true`. Then, and only then, each
vehicle gets CarConnectivity commands (charging, charge limit, climatization,
defrost, locks, trunk and frunk, lights and horn, wake; from 0.3.0 Sentry and the
alarm, windows, headlights, seat and steering wheel heat, Max A/C,
creature comfort, battery preconditioning, AC current limit) as set-hooks that run on
the caller's thread through the poller's own session. They are the only thing
that ever wakes the car, and they do it explicitly (LucidSession.command).
"""
# pylint: disable=protected-access
from __future__ import annotations
from typing import TYPE_CHECKING

import logging
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

from carconnectivity.attributes import DurationAttribute, EnumAttribute, GenericAttribute
from carconnectivity.command_impl import ChargingStartStopCommand, ClimatizationStartStopCommand, HonkAndFlashCommand, \
    LockUnlockCommand, WakeSleepCommand, WindowHeatingStartStopCommand
from carconnectivity.doors import Doors
from carconnectivity.windows import Windows
from carconnectivity.window_heating import WindowHeatings
from carconnectivity.drive import ElectricDrive, GenericDrive
from carconnectivity.enums import ConnectionState
from carconnectivity.errors import APICompatibilityError, AuthenticationError, CommandError, RetrievalError, \
    SetterError, TemporaryAuthenticationError, TooManyRequestsError
from carconnectivity.garage import Garage
from carconnectivity.units import Energy, Length, Power, Speed, Temperature
from carconnectivity.util import config_remove_credentials
from carconnectivity.vehicle import GenericVehicle

from carconnectivity_connectors.base.connector import BaseConnector
from carconnectivity_connectors.lucid import mapping
from carconnectivity_connectors.lucid.auth.session import LucidSession, translate_api_error
from carconnectivity_connectors.lucid.vehicle import AlarmCommand, LucidElectricVehicle, LucidVehicle, OnOffCommand, OpenCloseCommand, \
    LevelCommand, SeatClimateCommand, WindowsCommand
from carconnectivity_connectors.lucid._version import __version__

if TYPE_CHECKING:
    from typing import Any, Awaitable, Callable, Dict, Optional, Tuple, Union
    from carconnectivity.carconnectivity import CarConnectivity

LOG: logging.Logger = logging.getLogger("carconnectivity.connectors.lucid")
LOG_API: logging.Logger = logging.getLogger("carconnectivity.connectors.lucid-api-debug")

_dig = mapping._dig  # pylint: disable=protected-access
_f = mapping._f  # pylint: disable=protected-access

# The charge limit the Lucid app offers, whole percent.
CHARGE_LIMIT_MIN, CHARGE_LIMIT_MAX = 50, 100
# The AC charging current limit, whole amps. 8 A is the floor other Lucid clients already
# send; the 6 A an EVSE may signal is untried against the car, so it is not offered. 80 A
# is the Air's 19.2 kW onboard charger at 240 V.
AC_CURRENT_MIN, AC_CURRENT_MAX = 8, 80
# Plausible cabin set-points in Celsius: a guard against a unit mix-up (68 sent as C),
# not a claim about what the car accepts.
CABIN_TEMP_MIN_C, CABIN_TEMP_MAX_C = 10.0, 35.0
# A release notes request that failed is tried again after this, not on the next poll.
RELEASE_NOTES_RETRY_S = 3600.0
# The car takes a moment to report what a command did; poll once soon after rather
# than waiting out the interval. One extra read, which never wakes the car.
REFRESH_AFTER_COMMAND_S = 6.0


class Connector(BaseConnector):  # pylint: disable=too-many-instance-attributes
    """Connector class for Lucid vehicles."""

    def __init__(self, connector_id: str, car_connectivity: CarConnectivity, config: Dict, *args, initialization: Optional[Dict] = None,
                 **kwargs) -> None:
        BaseConnector.__init__(self, connector_id=connector_id, car_connectivity=car_connectivity, config=config, log=LOG, api_log=LOG_API,
                               *args, initialization=initialization, **kwargs)

        self._background_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        # Consecutive refusals from Lucid, which set how long the next wait is. Reset by
        # any poll that gets through; see mapping.rate_limit_backoff.
        self._rate_limited: int = 0
        # Cuts the poller's wait short when a command wants fresh state; shutdown sets it
        # too, so that wait ends with the stop event.
        self._poll_now = threading.Event()
        # The library's Vehicle message per VIN from the latest poll: a command needs its
        # vehicle_id, and its power state says whether the car is asleep.
        self._lucid_vehicles: Dict[str, Any] = {}
        # Release notes per VIN: (version, text, link) for the waiting version, fetched once
        # per version; and, per VIN and version, when a failed fetch may be tried again
        # (monotonic seconds), so a new version is not held back by an old one's failure.
        self._release_notes: Dict[str, Tuple[str, Optional[str], Optional[str]]] = {}
        self._release_notes_retry: Dict[Tuple[str, str], float] = {}

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

        # A separate, shorter interval for a car that is moving. At 60 s a drive is a
        # handful of points with straight lines between them. Defaults to `interval`, so
        # nothing changes unless it is asked for. The floor is lower than `interval`'s on
        # purpose: it only applies while driving, which is a small part of any day, and
        # the account is rate-limited far above that.
        self.active_config['driving_interval'] = self.active_config['interval']
        if 'driving_interval' in config:
            self.active_config['driving_interval'] = int(config['driving_interval'])
            if self.active_config['driving_interval'] < 15:
                raise ValueError('Driving interval must be at least 15 seconds')
            if self.active_config['driving_interval'] > self.active_config['interval']:
                raise ValueError('Driving interval must not be longer than interval')

        # Commands are off unless asked for, and only a real boolean asks: the string
        # "false" is truthy, and reading it as yes would unlock a car someone meant to
        # keep read-only.
        commands = config.get('commands', False)
        if not isinstance(commands, bool):
            raise ValueError('commands must be true or false')
        self.active_config['commands'] = commands
        if commands:
            LOG.warning('Commands are enabled: this connector can lock, unlock, open, disarm and wake the car')

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
        self._rate_limited = 0
        self.connection_state._set_value(value=ConnectionState.CONNECTING)  # pylint: disable=protected-access
        while not self._stop_event.is_set():
            interval: float = self.active_config['interval']
            started = time.monotonic()
            try:
                try:
                    if fetch:
                        self.fetch_all()
                        fetch = False
                    else:
                        self.update_vehicles()
                    LOG.debug('poll took %.1fs', time.monotonic() - started)
                    self.last_update._set_value(value=datetime.now(tz=timezone.utc))  # pylint: disable=protected-access
                    if self.interval.value is not None:
                        interval = self.interval.value.total_seconds()
                    interval = mapping.poll_interval(self._vehicle_states(), interval, self.active_config['driving_interval'])
                except Exception:
                    self.connection_state._set_value(value=ConnectionState.ERROR)  # pylint: disable=protected-access
                    if self.interval.value is not None:
                        interval = self.interval.value.total_seconds()
                    raise
            except TooManyRequestsError as err:
                self._rate_limited += 1
                wait = mapping.rate_limit_backoff(self._rate_limited)
                LOG.error('Lucid is rate-limiting this account (%s). Will try again after %.0fs', str(err), wait)
                self._stop_event.wait(wait)
            except (RetrievalError, APICompatibilityError, TemporaryAuthenticationError) as err:
                LOG.error('Error during update (%s). Will try again after %ss', str(err), interval)
                self._stop_event.wait(interval)
            except Exception as err:
                LOG.critical('Critical error during update: %s', traceback.format_exc())
                self.healthy._set_value(value=False)  # pylint: disable=protected-access
                self.connection_state._set_value(value=ConnectionState.ERROR)  # pylint: disable=protected-access
                raise err
            else:
                # A poll got through, so whatever the account was short of, it has it
                # again: the next refusal starts from one minute rather than from
                # wherever the last run of them ended up.
                self._rate_limited = 0
                self.connection_state._set_value(value=ConnectionState.CONNECTED)  # pylint: disable=protected-access
                # The interval is the spacing between polls, not the pause after one. A
                # fetch that takes fifteen seconds followed by a fifteen-second wait is a
                # thirty-second cycle, and that is what the recorder downstream measured:
                # 31 s between fixes while driving with this set to 15, 41 s at 20.
                self._poll_now.wait(max(0.0, interval - (time.monotonic() - started)))
                self._poll_now.clear()
        self._session.close()
        self.connection_state._set_value(value=ConnectionState.DISCONNECTED)  # pylint: disable=protected-access

    def _vehicle_states(self) -> list:
        """The state of each vehicle this connector manages, for choosing the next interval."""
        return [v.state.value for v in self.car_connectivity.garage.list_vehicles()
                if v.is_managed_by_connector(self) and v.state.enabled]

    def shutdown(self) -> None:
        for vehicle in self.car_connectivity.garage.list_vehicles():
            if len(vehicle.managing_connectors) == 1 and self in vehicle.managing_connectors:
                self.car_connectivity.garage.remove_vehicle(vehicle.id)
                vehicle.enabled = False
        self._stop_event.set()
        self._poll_now.set()
        if self._background_thread is not None:
            self._background_thread.join()
        BaseConnector.shutdown(self)

    # -- fetching --------------------------------------------------------------
    def fetch_all(self) -> None:
        """First fetch: vehicles and their full state."""
        self.fetch_vehicles()

    def update_vehicles(self) -> None:
        """Subsequent polls."""
        # One gRPC call returns every vehicle with full state, so "update" and "fetch" are the same request.
        self.fetch_vehicles()

    def fetch_vehicles(self) -> None:
        """Fetch every vehicle on the account, add new ones to the garage, drop ones that disappeared, apply state.

        Ends the transaction itself, at the end, on every poll. It used to be the caller's
        job and only fetch_all did it — which runs once, at startup. Observers registered
        with on_transaction_end=True therefore heard nothing after that first fetch, and
        the database plugin's trip agent is one of them, so no drive this connector ever
        watched became a trip. Doing it here means a new caller cannot forget."""
        garage: Garage = self.car_connectivity.garage
        seen: set[str] = set()
        lucid_vehicles: Dict[str, Any] = {}
        for lucid_vehicle in self._session.fetch_vehicles():
            vin: Optional[str] = _dig(lucid_vehicle, "config", "vin")
            if not vin:
                raise APICompatibilityError('Lucid vehicle without a VIN')
            if vin in self.active_config['hide_vins']:
                LOG.warning('Vehicle %s is hidden in config', vin)
                continue
            seen.add(vin)
            lucid_vehicles[vin] = lucid_vehicle
            vehicle: Optional[GenericVehicle] = garage.get_vehicle(vin)
            if vehicle is None:
                vehicle = LucidElectricVehicle(vin=vin, garage=garage, managing_connector=self, initialization=garage.get_initialization(vin))
                garage.add_vehicle(vin, vehicle)
            elif not isinstance(vehicle, LucidElectricVehicle):
                vehicle = LucidElectricVehicle(garage=garage, origin=vehicle)
                garage.replace_vehicle(vin, vehicle)
            if self.active_config['commands']:
                self._add_commands(vehicle)
            self._apply(vehicle, lucid_vehicle)
            self._apply_release_notes(vehicle, lucid_vehicle)
        self._lucid_vehicles = lucid_vehicles
        for vin in set(garage.list_vehicle_vins()) - seen:
            gone = garage.get_vehicle(vin)
            if gone is not None and gone.is_managed_by_connector(self):
                garage.remove_vehicle(vin)
        # Deliver everything this poll changed to the observers that wait for a whole
        # update rather than each attribute: notify() has been accumulating their flags
        # since the last transaction_end, and nothing else calls it.
        self.car_connectivity.transaction_end()

    def _apply_release_notes(self, vehicle: GenericVehicle, lucid_vehicle: Any) -> None:
        """Release notes for the waiting update: one request per version, kept until the
        version changes, never one per poll. A failed request is logged and tried again an
        hour later; the poll goes on either way."""
        if not isinstance(vehicle, LucidVehicle):
            return
        vin: str = vehicle.vin.value
        version = mapping.waiting_update(_dig(lucid_vehicle, "state", "software_update"))
        if not mapping.release_notes_apply(_dig(lucid_vehicle, "config", "model")):
            version = None  # never another model's notes
        notes: Optional[str] = None
        link: Optional[str] = None
        if version is not None and vin in self._release_notes and self._release_notes[vin][0] == version:
            _, notes, link = self._release_notes[vin]
        elif version is not None and time.monotonic() >= self._release_notes_retry.get((vin, version), 0.0):
            try:
                notes, link = mapping.release_notes(self._session.release_notes(lucid_vehicle, version))
            except Exception as err:  # pylint: disable=broad-except
                # Rate limits included: a missing note is not worth the poll's backoff.
                LOG.warning('Release notes for %s could not be fetched (%s); trying again in %.0fs', version, err, RELEASE_NOTES_RETRY_S)
                self._release_notes_retry[(vin, version)] = time.monotonic() + RELEASE_NOTES_RETRY_S
            else:
                self._release_notes[vin] = (version, notes, link)
                self._release_notes_retry = {key: at for key, at in self._release_notes_retry.items() if key[0] != vin}
        vehicle.software_update_release_notes._set_value(notes)
        vehicle.software_update_release_notes_url._set_value(link)

    # -- commands --------------------------------------------------------------
    def _add_commands(self, vehicle: GenericVehicle) -> None:
        """Give the vehicle its commands, a settable charge limit and AC current limit; safe to call every poll.
        Only called when the config enables commands."""
        if not isinstance(vehicle, LucidElectricVehicle):
            return
        wanted = (
            (vehicle.charging.commands, ChargingStartStopCommand, 'start-stop', self._on_charging_start_stop),
            (vehicle.climatization.commands, ClimatizationStartStopCommand, 'start-stop', self._on_climatization_start_stop),
            (vehicle.window_heatings.commands, WindowHeatingStartStopCommand, 'start-stop', self._on_defrost),
            (vehicle.doors.commands, LockUnlockCommand, 'lock-unlock', self._on_lock_unlock),
            (vehicle.doors.commands, OpenCloseCommand, 'trunk', self._on_trunk),
            (vehicle.doors.commands, OpenCloseCommand, 'frunk', self._on_frunk),
            (vehicle.commands, HonkAndFlashCommand, 'honk-flash', self._on_honk_flash),
            (vehicle.commands, WakeSleepCommand, 'wake-sleep', self._on_wake_sleep),
            # 0.3.0: Halo Secure.
            (vehicle.commands, OnOffCommand, 'sentry', self._choice_hook({
                OnOffCommand.Command.ON: ('turn on sentry', lambda api, v: api.sentry_mode_on(v)),
                OnOffCommand.Command.OFF: ('turn off sentry', lambda api, v: api.sentry_mode_off(v))})),
            (vehicle.commands, OnOffCommand, 'enhanced-deterrence', self._choice_hook({
                OnOffCommand.Command.ON: ('turn on enhanced deterrence', lambda api, v: api.enhanced_deterrence_on(v)),
                OnOffCommand.Command.OFF: ('turn off enhanced deterrence', lambda api, v: api.enhanced_deterrence_off(v))})),
            (vehicle.commands, OnOffCommand, 'sentry-at-home', self._choice_hook({
                OnOffCommand.Command.ON: ('turn on sentry at home', lambda api, v: api.sentry_mode_at_home_on(v)),
                OnOffCommand.Command.OFF: ('turn off sentry at home', lambda api, v: api.sentry_mode_at_home_off(v))})),
            (vehicle.commands, OnOffCommand, 'sentry-at-work', self._choice_hook({
                OnOffCommand.Command.ON: ('turn on sentry at work', lambda api, v: api.sentry_mode_at_work_on(v)),
                OnOffCommand.Command.OFF: ('turn off sentry at work', lambda api, v: api.sentry_mode_at_work_off(v))})),
            # An alarm cannot be set off remotely, only silenced.
            (vehicle.commands, OnOffCommand, 'sentry-alarm', self._choice_hook({
                OnOffCommand.Command.OFF: ('silence the sentry alarm', lambda api, v: api.turn_off_sentry_alarm(v))})),
            (vehicle.commands, AlarmCommand, 'alarm', self._choice_hook({
                choice: (f'set the alarm {choice}', lambda api, v, mode=mode: api.alarm_control(v, mode))
                for choice, mode in ((AlarmCommand.Command.OFF, 1), (AlarmCommand.Command.ON, 2), (AlarmCommand.Command.SILENT, 3))})),
            # Windows, headlights.
            (vehicle.commands, WindowsCommand, 'windows', self._choice_hook({
                WindowsCommand.Command.VENT: ('vent windows', lambda api, v: api.open_all_windows(v)),
                WindowsCommand.Command.CLOSE: ('close windows', lambda api, v: api.close_all_windows(v))})),
            (vehicle.commands, OnOffCommand, 'lights', self._choice_hook({
                OnOffCommand.Command.ON: ('turn on headlights', lambda api, v: api.lights_on(v)),
                OnOffCommand.Command.OFF: ('turn off headlights', lambda api, v: api.lights_off(v))})),
            # Climate beyond the cabin temperature.
            (vehicle.climatization.commands, OnOffCommand, 'max-ac', self._choice_hook({
                OnOffCommand.Command.ON: ('turn on max a/c', lambda api, v: api.max_ac_on(v)),
                OnOffCommand.Command.OFF: ('turn off max a/c', lambda api, v: api.max_ac_off(v))})),
            (vehicle.climatization.commands, OnOffCommand, 'creature-comfort', self._choice_hook({
                choice: (f'turn {choice} creature comfort', lambda api, v, mode=mode: api.creature_comfort_control(v, mode))
                for choice, mode in ((OnOffCommand.Command.ON, mapping.CREATURE_COMFORT_MODE[True]),
                                     (OnOffCommand.Command.OFF, mapping.CREATURE_COMFORT_MODE[False]))})),
            (vehicle.climatization.commands, LevelCommand, 'steering-wheel-heater', self._choice_hook({
                choice: (f'set steering wheel heater {choice}', lambda api, v, level=level: api.steering_wheel_heater_control(v, level))
                for choice, level in mapping.STEERING_LEVEL.items()}, refusal=mapping.steering_heater_refusal)),
            (vehicle.climatization.commands, SeatClimateCommand, 'seat-climate', self._on_seat_climate),
            (vehicle.charging.commands, OnOffCommand, 'battery-preconditioning', self._choice_hook({
                OnOffCommand.Command.ON: ('turn on battery preconditioning', lambda api, v: api.battery_precon_on(v)),
                OnOffCommand.Command.OFF: ('turn off battery preconditioning', lambda api, v: api.battery_precon_off(v))})),
        )
        for commands, kind, name, hook in wanted:
            if not commands.contains_command(name):
                command = kind(name=name, parent=commands)
                command._add_on_set_hook(hook)
                command.enabled = True
                commands.add_command(command)
        target = vehicle.charging.settings.target_level
        if not target._is_changeable:
            target.minimum, target.maximum, target.precision = float(CHARGE_LIMIT_MIN), float(CHARGE_LIMIT_MAX), 1.0
            target._add_on_set_hook(self._on_target_level)
            target._is_changeable = True
        current = vehicle.charging.settings.maximum_current
        if not current._is_changeable:
            current.minimum, current.maximum, current.precision = float(AC_CURRENT_MIN), float(AC_CURRENT_MAX), 1.0
            current._add_on_set_hook(self._on_maximum_current)
            current._is_changeable = True

    @staticmethod
    def _vehicle_of(obj: Any) -> LucidElectricVehicle:
        """Walk up from a command or attribute to the vehicle that owns it."""
        node = obj
        while node is not None and not isinstance(node, GenericVehicle):
            node = getattr(node, 'parent', None)
        if not isinstance(node, LucidElectricVehicle) or not node.vin.value:
            raise CommandError('Object hierarchy is not as expected')
        return node

    def _send(self, vehicle: LucidElectricVehicle, what: str, action: Callable[[Any, Any], Awaitable[None]], wake: bool = True,
              error: type = CommandError) -> None:
        """Run one command on the shared session, once, then ask for a refresh. Only the
        last four of the VIN are logged; no token or position ever is."""
        if not self.active_config['commands']:
            # Nothing should reach here with commands off, since no command object exists;
            # this is the second lock on the same door.
            raise error('Commands are disabled in the Lucid connector config')
        vin: str = vehicle.vin.value
        lucid_vehicle = self._lucid_vehicles.get(vin)
        if lucid_vehicle is None:
            raise error('Vehicle has not been fetched from Lucid yet')
        LOG.info('Command %s for vehicle ...%s', what, vin[-4:])
        asleep = not mapping.is_awake(_dig(lucid_vehicle, "state", "power"), _dig(lucid_vehicle, "state", "charging", "charge_state"))
        try:
            self._session.command(lucid_vehicle, action, wake_first=wake and asleep)
        except (RetrievalError, AuthenticationError, RuntimeError) as err:
            LOG.error('Command %s for vehicle ...%s failed: %s', what, vin[-4:], err)
            raise error(f'{what} failed: {err}') from err
        LOG.info('Command %s for vehicle ...%s accepted', what, vin[-4:])
        timer = threading.Timer(REFRESH_AFTER_COMMAND_S, self._poll_now.set)
        timer.daemon = True
        timer.start()

    def _config_of(self, vehicle: LucidElectricVehicle) -> Any:
        """The car's VehicleConfig from the latest poll: what it is fitted with."""
        return _dig(self._lucid_vehicles.get(vehicle.vin.value), "config")

    @staticmethod
    def _refuse_if(reason: Optional[str]) -> None:
        """Refuse a command the car's own config says it cannot do, before anything is sent."""
        if reason is not None:
            raise CommandError(f'Not sent: {reason}')

    @staticmethod
    def _command_name(command_arguments: Union[str, Dict[str, Any]], enum: type) -> Any:
        if not isinstance(command_arguments, dict) or not isinstance(command_arguments.get('command'), enum):
            raise CommandError(f'Command arguments are not as expected: {command_arguments}')
        return command_arguments['command']

    def _choice_hook(self, actions: Dict[Any, Tuple[str, Callable[[Any, Any], Awaitable[None]]]],
                     refusal: Optional[Callable[[Any], Optional[str]]] = None) \
            -> Callable[[Any, Union[str, Dict[str, Any]]], Union[str, Dict[str, Any]]]:
        """A set-hook for one of the connector's own choice commands: each choice it takes
        is a (what, action) pair sent like any other command; a choice not in the table,
        the "on" of an alarm that can only be silenced, is refused before anything is sent,
        and so is any choice when `refusal`, given the car's config, names a reason."""
        enum = type(next(iter(actions)))

        def hook(command: Any, command_arguments: Union[str, Dict[str, Any]]) -> Union[str, Dict[str, Any]]:
            vehicle = self._vehicle_of(command)
            choice = self._command_name(command_arguments, enum)
            if choice not in actions:
                raise CommandError(f'{command.name} takes only {", ".join(str(c) for c in actions)}')
            self._refuse_if(refusal(self._config_of(vehicle)) if refusal is not None else None)
            what, action = actions[choice]
            self._send(vehicle, what, action)
            return command_arguments
        return hook

    def _on_charging_start_stop(self, command: ChargingStartStopCommand, command_arguments: Union[str, Dict[str, Any]]) -> Union[str, Dict[str, Any]]:
        vehicle = self._vehicle_of(command)
        if self._command_name(command_arguments, ChargingStartStopCommand.Command) == ChargingStartStopCommand.Command.START:
            self._send(vehicle, 'start charging', lambda api, v: api.start_charging(v))
        else:
            self._send(vehicle, 'stop charging', lambda api, v: api.stop_charging(v))
        return command_arguments

    def _on_climatization_start_stop(self, command: ClimatizationStartStopCommand, command_arguments: Union[str, Dict[str, Any]]) \
            -> Union[str, Dict[str, Any]]:
        vehicle = self._vehicle_of(command)
        if self._command_name(command_arguments, ClimatizationStartStopCommand.Command) == ClimatizationStartStopCommand.Command.STOP:
            self._send(vehicle, 'stop climatization', lambda api, v: api.set_cabin_temperature(v, None))
            return command_arguments
        temperature = command_arguments.get('target_temperature')
        unit = command_arguments.get('target_temperature_unit', Temperature.C)
        if temperature is None:
            temperature, unit = vehicle.climatization.settings.target_temperature.value, Temperature.C
        if temperature is None:
            raise CommandError('Starting climatization needs a target_temperature; the car has not reported one')
        try:
            # Over MQTT or REST the unit is JSON, so the string '°F' rather than the enum.
            celsius = {Temperature.C: lambda t: t, Temperature.F: lambda t: (t - 32.0) * 5.0 / 9.0,
                       Temperature.K: lambda t: t - 273.15}[Temperature(unit)](float(temperature))
        except (KeyError, TypeError, ValueError) as err:
            raise CommandError(f'Unusable target temperature {temperature} {unit}') from err
        if not CABIN_TEMP_MIN_C <= celsius <= CABIN_TEMP_MAX_C:
            raise CommandError(f'Target temperature {celsius:.1f} C is outside {CABIN_TEMP_MIN_C:.0f}-{CABIN_TEMP_MAX_C:.0f} C')
        self._send(vehicle, 'start climatization', lambda api, v: api.set_cabin_temperature(v, round(celsius, 1)))
        return command_arguments

    def _on_defrost(self, command: WindowHeatingStartStopCommand, command_arguments: Union[str, Dict[str, Any]]) -> Union[str, Dict[str, Any]]:
        vehicle = self._vehicle_of(command)
        if self._command_name(command_arguments, WindowHeatingStartStopCommand.Command) == WindowHeatingStartStopCommand.Command.START:
            self._send(vehicle, 'start defrost', lambda api, v: api.defrost_on(v))
        else:
            self._send(vehicle, 'stop defrost', lambda api, v: api.defrost_off(v))
        return command_arguments

    def _on_lock_unlock(self, command: LockUnlockCommand, command_arguments: Union[str, Dict[str, Any]]) -> Union[str, Dict[str, Any]]:
        vehicle = self._vehicle_of(command)
        if self._command_name(command_arguments, LockUnlockCommand.Command) == LockUnlockCommand.Command.LOCK:
            self._send(vehicle, 'lock doors', lambda api, v: api.doors_lock(v))
        else:
            self._send(vehicle, 'unlock doors', lambda api, v: api.doors_unlock(v))
        return command_arguments

    def _on_trunk(self, command: OpenCloseCommand, command_arguments: Union[str, Dict[str, Any]]) -> Union[str, Dict[str, Any]]:
        vehicle = self._vehicle_of(command)
        if self._command_name(command_arguments, OpenCloseCommand.Command) == OpenCloseCommand.Command.OPEN:
            self._send(vehicle, 'open trunk', lambda api, v: api.trunk_open(v))
        else:
            self._send(vehicle, 'close trunk', lambda api, v: api.trunk_close(v))
        return command_arguments

    def _on_frunk(self, command: OpenCloseCommand, command_arguments: Union[str, Dict[str, Any]]) -> Union[str, Dict[str, Any]]:
        vehicle = self._vehicle_of(command)
        if self._command_name(command_arguments, OpenCloseCommand.Command) == OpenCloseCommand.Command.OPEN:
            self._send(vehicle, 'open frunk', lambda api, v: api.frunk_open(v))
        else:
            self._refuse_if(mapping.frunk_close_refusal(self._config_of(vehicle)))
            self._send(vehicle, 'close frunk', lambda api, v: api.frunk_close(v))
        return command_arguments

    def _on_honk_flash(self, command: HonkAndFlashCommand, command_arguments: Union[str, Dict[str, Any]]) -> Union[str, Dict[str, Any]]:
        vehicle = self._vehicle_of(command)

        async def honk_and_flash(api: Any, v: Any) -> None:
            await api.honk_horn(v)
            try:
                await api.lights_flash(v)
            except Exception as err:  # pylint: disable=broad-except
                # Say the horn went, or a retry of honk-and-flash honks a second time.
                raise RetrievalError(f'the horn sounded but the lights did not flash ({translate_api_error(err)}); '
                                     'retry with flash alone') from err
        if self._command_name(command_arguments, HonkAndFlashCommand.Command) == HonkAndFlashCommand.Command.FLASH:
            self._send(vehicle, 'flash lights', lambda api, v: api.lights_flash(v))
        else:
            self._send(vehicle, 'honk and flash', honk_and_flash)
        return command_arguments

    def _on_wake_sleep(self, command: WakeSleepCommand, command_arguments: Union[str, Dict[str, Any]]) -> Union[str, Dict[str, Any]]:
        vehicle = self._vehicle_of(command)
        if self._command_name(command_arguments, WakeSleepCommand.Command) != WakeSleepCommand.Command.WAKE:
            raise CommandError('A Lucid cannot be put to sleep remotely; it sleeps on its own')
        # The wake is the command, so the usual pre-wake would only do it twice.
        self._send(vehicle, 'wake', lambda api, v: api.wakeup_vehicle(v), wake=False)
        return command_arguments

    def _on_seat_climate(self, command: SeatClimateCommand, command_arguments: Union[str, Dict[str, Any]]) -> Union[str, Dict[str, Any]]:
        vehicle = self._vehicle_of(command)
        if not isinstance(command_arguments, dict):
            raise CommandError(f'Command arguments are not as expected: {command_arguments}')
        seat, mode, level = command_arguments.get('seat'), command_arguments.get('mode'), command_arguments.get('level')
        zones = mapping.SEAT_ZONES.get((seat, mode))
        if zones is None or level not in mapping.SEAT_MODE:
            raise CommandError(f'Command arguments are not as expected: {command_arguments}')
        self._refuse_if(mapping.seat_refusal(self._config_of(vehicle), seat, mode))
        car_mode = mapping.SEAT_MODE[level]
        self._send(vehicle, f'set {seat} seat {mode} {level}',
                   lambda api, v: api.seat_climate_control(v, **{zone: car_mode for zone in zones}))
        return command_arguments

    def _on_target_level(self, attribute: GenericAttribute, value: Any) -> Any:
        try:
            vehicle = self._vehicle_of(attribute)
        except CommandError as err:
            raise SetterError(str(err)) from err
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not CHARGE_LIMIT_MIN <= value <= CHARGE_LIMIT_MAX \
                or float(value) != int(value):
            raise SetterError(f'Charge limit must be a whole percent from {CHARGE_LIMIT_MIN} to {CHARGE_LIMIT_MAX}, not {value!r}')
        self._send(vehicle, f'set charge limit to {int(value)}%', lambda api, v: api.set_charge_limit(v, int(value)), error=SetterError)
        return value

    def _on_maximum_current(self, attribute: GenericAttribute, value: Any) -> Any:
        try:
            vehicle = self._vehicle_of(attribute)
        except CommandError as err:
            raise SetterError(str(err)) from err
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not AC_CURRENT_MIN <= value <= AC_CURRENT_MAX \
                or float(value) != int(value):
            raise SetterError(f'AC current limit must be whole amps from {AC_CURRENT_MIN} to {AC_CURRENT_MAX}, not {value!r}')
        self._send(vehicle, f'set AC current limit to {int(value)} A', lambda api, v: api.set_ac_current_limit(v, int(value)), error=SetterError)
        return value

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
        self._apply_speed(vehicle, st, measured)
        self._apply_doors(vehicle, st, measured)
        self._apply_windows(vehicle, st, measured)
        self._apply_cabin(vehicle, st, measured)
        self._apply_security(vehicle, st, measured)
        self._apply_tires(vehicle, st, measured)
        self._apply_climate(vehicle, st, measured)
        self._apply_window_heating(vehicle, st, measured)
        self._apply_controls(vehicle, st, measured)

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
        # Whether a cable is in, and whether it is delivering. Read off the charge state
        # rather than body.charge_port, which is the port's door and says nothing about
        # a cable. A plug-in reminder downstream waits on exactly this attribute.
        sv.charging.connector.connection_state._set_value(mapping.connector_connection_state(cs), measured=measured)
        sv.charging.connector.external_power._set_value(mapping.external_power(cs), measured=measured)
        # The AC current limit as set, which the car keeps between sessions; the active
        # session's own limit can be lower, held down by the charger. 0 is the car not saying.
        ac_limit = _f(_dig(ch, "energy_ac_current_limit"))
        sv.charging.settings.maximum_current._set_value(ac_limit if ac_limit else None, measured=measured)
        sv.charging.connector.lock_state._set_value(mapping.cable_lock(_dig(ch, "cable_lock"), cs), measured=measured)
        # The car's own energy meter for this session, and when it will reach the limit.
        sv.charge_session_energy._set_value(mapping.charge_session_energy(cs, _dig(ch, "charge_session_kwh")), measured=measured, unit=Energy.KWH)
        sv.charging.estimated_date_reached._set_value(mapping.charge_finish(measured, cs, _dig(ch, "session_minutes_remaining")), measured=measured)

    @staticmethod
    def _apply_position(sv: LucidVehicle, st: Any, measured: datetime) -> None:
        gps = _dig(st, "gps")
        lat, lon = _f(_dig(gps, "location", "latitude")), _f(_dig(gps, "location", "longitude"))
        if not mapping.has_fix(lat, lon):
            return
        sv.position.latitude._set_value(lat, measured=measured)
        sv.position.longitude._set_value(lon, measured=measured)
        # gps.elevation is not published: a real Gravity sent 8932 where cars beside it
        # record 123 m, so its unit is not metres, feet, decimetres or centimetres.
        sv.position.heading._set_value(_f(_dig(gps, "heading_precise")), measured=measured)
        sv.position.position_type._set_value(mapping.position_type(_dig(st, "power")), measured=measured)

    @staticmethod
    def _apply_speed(sv: LucidVehicle, st: Any, measured: datetime) -> None:
        # chassis.speed is km/h despite what the proto says; see mapping.speed_kmh.
        sv.speed._set_value(mapping.speed_kmh(_dig(st, "chassis", "speed")), measured=measured)

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
    def _apply_windows(sv: LucidVehicle, st: Any, measured: datetime) -> None:
        """Four windows, the same way the doors are done: only the ones that reported
        are summarised, so an absent body block is "unknown" rather than "all closed"."""
        positions = _dig(st, "body", "window_position")
        any_open = False
        reported = False
        for window_id, field in mapping.WINDOWS.items():
            raw = _dig(positions, field)
            if raw is None:
                continue
            if window_id in sv.windows.windows:
                window = sv.windows.windows[window_id]
            else:
                window = Windows.Window(window_id=window_id, windows=sv.windows, initialization=sv.windows.get_initialization(window_id))
                sv.windows.windows[window_id] = window
            state = mapping.window_open_state(raw)
            window.open_state._set_value(state, measured=measured)
            any_open = any_open or state in (Windows.OpenState.OPEN, Windows.OpenState.AJAR)
            reported = True
        if reported:
            sv.windows.open_state._set_value(Windows.OpenState.OPEN if any_open else Windows.OpenState.CLOSED, measured=measured)
        else:
            sv.windows.open_state._set_value(Windows.OpenState.UNKNOWN, measured=measured)

    @staticmethod
    def _apply_cabin(sv: LucidVehicle, st: Any, measured: datetime) -> None:
        """Cabin temperature and whether Sentry is watching; both connector-specific."""
        sv.inside_temperature._set_value(mapping.plausible_temp(_f(_dig(st, "cabin", "interior_temp"))), measured=measured, unit=Temperature.C)
        sv.sentry._set_value(mapping.sentry_armed(_dig(st, "sentry_state", "enablement_state")), measured=measured)
        sv.sentry_threat._set_value(mapping.sentry_threat(_dig(st, "sentry_state", "threat_level")), measured=measured)
        battery = _dig(st, "battery")
        sv.battery_capacity_kwh._set_value(_f(_dig(battery, "capacity_kwhr")), measured=measured)
        sv.battery_energy_kwh._set_value(_f(_dig(battery, "kwhr")), measured=measured)
        sv.battery_health_warning._set_value(mapping.warning_flag(_dig(battery, "battery_health")), measured=measured)
        # The car's raw figure beside that warning. Very likely a state-of-health percentage,
        # but the proto does not say so, so it is published as the car's number under the
        # proto's name. 0 is the car not saying.
        sv.battery_health_level._set_value(mapping.nonzero(_dig(battery, "battery_health_level")), measured=measured)
        software_update = _dig(st, "software_update")
        version = mapping.waiting_update(software_update)
        sv.software_update_version._set_value(version, measured=measured)
        install = mapping.install_time(_dig(software_update, "install_duration_minutes")) if version is not None else None
        sv.software_update_install_time._set_value(install, measured=measured)
        sv.software_update_available._set_value(mapping.update_available(_dig(st, "software_update", "update_available")), measured=measured)

    @staticmethod
    def _apply_security(sv: LucidVehicle, st: Any, measured: datetime) -> None:
        """Why Sentry is paused, its drive, and the warnings worth a phone alert; all connector-specific."""
        sentry = _dig(st, "sentry_state")
        sv.sentry_inactive_reason._set_value(mapping.sentry_inactive_reason(sentry), measured=measured)
        usb = _dig(sentry, "usb_drive_status")
        sv.sentry_usb_drive_connected._set_value(mapping.two_state(usb, mapping.USB_CONNECTED, mapping.USB_NOT_CONNECTED), measured=measured)
        body = _dig(st, "body")
        sv.living_object_detection._set_value(mapping.pick(mapping.LIVING_OBJECT_DETECTION, _dig(body, "living_object_detection_status")),
                                              measured=measured)
        sv.unattended_occupant_detection._set_value(
            mapping.pick(mapping.UNATTENDED_OCCUPANT_DETECTION, _dig(body, "unattended_occupant_detection_status")), measured=measured)
        sv.keyfob_battery_low._set_value(mapping.two_state(_dig(body, "keyfob_battery_status"), mapping.KEYFOB_LOW, mapping.KEYFOB_OK),
                                         measured=measured)

    @staticmethod
    def _apply_tires(sv: LucidVehicle, st: Any, measured: datetime) -> None:
        """Four pressures in bar and the car's own warning, from ChassisState."""
        chassis = _dig(st, "chassis")
        for wheel in mapping.TIRES:
            getattr(sv, f"tire_pressure_{wheel}")._set_value(_f(_dig(chassis, f"{wheel}_tire_pressure_bar")), measured=measured)
        flags = [_dig(chassis, f"{kind}_warn_{side}") for kind in ("hard", "soft") for side in ("left_front", "left_rear", "right_front", "right_rear")]
        sv.tire_warning._set_value(mapping.tire_warning(flags), measured=measured)

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

    @staticmethod
    def _apply_window_heating(sv: LucidVehicle, st: Any, measured: datetime) -> None:
        """Defrost and the rear window heater as window_heatings, the state behind the
        defrost command. A heater entry exists once it has reported."""
        hvac = _dig(st, "hvac")
        heaters = {"front": mapping.two_state(_dig(hvac, "defrost"), mapping.DEFROST_ON, mapping.DEFROST_OFF),
                   "rear": mapping.two_state(_dig(hvac, "rear_window_heating_status"), mapping.REAR_WINDOW_HEAT_ON, mapping.REAR_WINDOW_HEAT_OFF)}
        for window_id, on in heaters.items():
            if on is None:
                if window_id in sv.window_heatings.windows:
                    sv.window_heatings.windows[window_id].heating_state._set_value(WindowHeatings.HeatingState.UNKNOWN, measured=measured)
                continue
            if window_id not in sv.window_heatings.windows:
                sv.window_heatings.windows[window_id] = WindowHeatings.WindowHeating(
                    window_id=window_id, window_heatings=sv.window_heatings, initialization=sv.window_heatings.get_initialization(window_id))
            state = WindowHeatings.HeatingState.ON if on else WindowHeatings.HeatingState.OFF
            sv.window_heatings.windows[window_id].heating_state._set_value(state, measured=measured)
        # On if any heater is on; off only when every heater says off; otherwise unknown, since a
        # heater that did not say may be the one that is on.
        if any(heaters.values()):
            overall = WindowHeatings.HeatingState.ON
        elif all(on is False for on in heaters.values()):
            overall = WindowHeatings.HeatingState.OFF
        else:
            overall = WindowHeatings.HeatingState.UNKNOWN
        sv.window_heatings.heating_state._set_value(overall, measured=measured)

    @staticmethod
    def _apply_controls(sv: LucidVehicle, st: Any, measured: datetime) -> None:
        """The state behind the 0.3.0 commands, each where a UI would look for its toggle."""
        # EnhancedDeterrenceState shares SentryEnablementState's numbers: 1 enabled, 2 disabled, 3 idle.
        sv.enhanced_deterrence._set_value(mapping.sentry_armed(_dig(st, "sentry_state", "enhanced_deterrence_state")), measured=measured)
        sv.alarm._set_value(mapping.alarm_mode(_dig(st, "alarm", "mode")), measured=measured)
        sv.alarm_status._set_value(mapping.pick(mapping.ALARM_STATUS, _dig(st, "alarm", "status")), measured=measured)
        sv.charge_port._set_value(mapping.door_open_state(_dig(st, "body", "charge_port")), measured=measured)
        sv.lights.light_state._set_value(mapping.headlights(_dig(st, "chassis", "headlights")), measured=measured)
        sv.battery_preconditioning._set_value(mapping.two_state(_dig(st, "battery", "preconditioning_status"), mapping.PRECON_ON, mapping.PRECON_OFF),
                                              measured=measured)
        hvac = _dig(st, "hvac")
        sv.max_ac._set_value(mapping.two_state(_dig(hvac, "max_ac_status"), mapping.MAX_AC_ON, mapping.MAX_AC_OFF), measured=measured)
        sv.creature_comfort._set_value(mapping.two_state(_dig(hvac, "keep_climate_status"), mapping.CREATURE_COMFORT_ON, mapping.CREATURE_COMFORT_OFF),
                                       measured=measured)
        sv.steering_wheel_heater._set_value(mapping.steering_wheel_level(_dig(hvac, "steering_heater_level"), _dig(hvac, "steering_heater")), measured=measured)
        for (seat, mode), zones in mapping.SEAT_ZONES.items():
            level = mapping.seat_level(_dig(hvac, "seats", zone) for zone in zones)
            sv.seat_climate[mapping.seat_attribute(seat, mode)]._set_value(level, measured=measured)

    # -- identity --------------------------------------------------------------
    def get_version(self) -> str:
        return __version__

    def get_type(self) -> str:
        return "carconnectivity-connector-lucid"

    def get_name(self) -> str:
        return "Lucid Connector"
