# CarConnectivity-connector-lucid

A [CarConnectivity](https://github.com/tillsteinbach/CarConnectivity) connector for Lucid Motors vehicles (Air and Gravity), built on [python-lucidmotors](https://github.com/nshp/python-lucidmotors).

**Read-only by default.** It polls the same mobile API the Lucid app uses and populates the CarConnectivity model: state, odometer, battery level / range / capacity / cell temperatures, charging state, target and time to finish, position, doors and locks, windows, climatization, window heating, software version and the waiting update. It also reads what the car says about itself that the shared model has no slot for (see [What else it reads](#what-else-it-reads)). Reads never wake the car, so polling costs nothing in battery drain. Commands (lock, climate, charging, trunk and the rest from 0.2.0; Halo Secure, windows, seats and more from 0.3.0) but stay off unless the config turns them on; see [Commands](#commands).

Verified against a 2026 Gravity on 2026-09-03.

**Unofficial.** This project is not made, endorsed or supported by Lucid Motors. It talks to the API the Lucid app uses, which Lucid can change or close at any time. Keep its polling modest (see [Polling budget](#polling-budget)) so it stays a good neighbour.


## Install

```
pip install carconnectivity-connector-lucid
```

Python 3.12 or newer. The connector logs in with a refresh token only (`login_with_refresh_token()`, in `lucidmotors` 1.4.2 and later), so there is **no password on disk**.

## Credentials

The connector needs only a Lucid **refresh token**, stored in a JSON file:

```json
{"refresh_token": "…"}
```

Mint one once with the library's login example (username + password, interactively), save the `refresh_token` it returns, and never store the password. The token does not rotate, so the file can be mounted read-only. Point the config at it — never paste the token into the config file itself.

## Config

```json
{
    "carConnectivity": {
        "connectors": [
            {
                "type": "lucid",
                "config": {
                    "refresh_token_file": "~/.config/lucid-token.json",
                    "interval": 60,
                    "driving_interval": 15
                }
            }
        ]
    }
}
```

All options in [doc/Config.md](doc/Config.md).

## Commands

Off by default. Without the option the connector creates no command objects at all and the charge limit is read-only, so nothing CarConnectivity exposes (the REST API, MQTT, the web UI) can send anything to the car. To turn them on, add `"commands": true` to the connector's `config`. It must be the JSON boolean `true`; a string such as `"true"` is refused at startup.

| command | where | values | library call |
|---|---|---|---|
| Charging | `charging/commands/start-stop` | `start`, `stop` | `start_charging` / `stop_charging` |
| Charge limit | `charging/settings/target_level` | whole percent, 50 to 100 | `set_charge_limit` |
| Climatization | `climatization/commands/start-stop` | `start [--target-temperature 21]`, `stop` | `set_cabin_temperature` (precondition / off) |
| Defrost | `window_heating/commands/start-stop` | `start`, `stop` | `defrost_on` / `defrost_off` |
| Locks | `doors/commands/lock-unlock` | `lock`, `unlock` | `doors_lock` / `doors_unlock` |
| Trunk | `doors/commands/trunk` | `open`, `close` | `trunk_open` / `trunk_close` |
| Frunk | `doors/commands/frunk` | `open`, `close` | `frunk_open` / `frunk_close` |
| Lights and horn | `commands/honk-flash` | `flash`, `honk-and-flash` | `lights_flash`, `honk_horn` |
| Wake | `commands/wake-sleep` | `wake` (a Lucid cannot be sent to sleep) | `wakeup_vehicle` |
| Sentry (Halo Secure) | `commands/sentry` | `on`, `off` | `sentry_mode_on` / `sentry_mode_off` |
| Enhanced deterrence | `commands/enhanced-deterrence` | `on`, `off` | `enhanced_deterrence_on` / `_off` |
| Sentry at home | `commands/sentry-at-home` | `on`, `off` | `sentry_mode_at_home_on` / `_off` |
| Sentry at work | `commands/sentry-at-work` | `on`, `off` | `sentry_mode_at_work_on` / `_off` |
| Silence a Sentry alarm | `commands/sentry-alarm` | `off` (an alarm cannot be set off remotely) | `turn_off_sentry_alarm` |
| Security alarm | `commands/alarm` | `on`, `off`, `silent` (notifications only) | `alarm_control` |
| Windows, all four | `commands/windows` | `vent` (drops them a little; again drops them further), `close` | `open_all_windows` / `close_all_windows` |
| Headlights | `commands/lights` | `on`, `off` | `lights_on` / `lights_off` |
| Max A/C | `climatization/commands/max-ac` | `on`, `off` | `max_ac_on` / `max_ac_off` |
| Creature comfort | `climatization/commands/creature-comfort` | `on`, `off` | `creature_comfort_control` |
| Seat heat and ventilation | `climatization/commands/seat-climate` | `<seat> <heat\|vent> <off\|low\|medium\|high>`, seat `driver`, `front-passenger`, `rear-left`, `rear-center`, `rear-right`; rear seats heat only | `seat_climate_control` (every zone of that seat) |
| Steering wheel heater | `climatization/commands/steering-wheel-heater` | `off`, `low`, `medium`, `high` | `steering_wheel_heater_control` |
| Battery preconditioning | `charging/commands/battery-preconditioning` | `on`, `off` | `battery_precon_on` / `battery_precon_off` |
| AC current limit | `charging/settings/maximum_current` | whole amps, 8 to 80 | `set_ac_current_limit` |
| Charging schedule | `charging/commands/charging-schedule` | `off`, `on` (keeps the car's times), `23:00-06:00` (on, with those times; `23:00-` or `-06:00` changes one end), or `{"on": true, "start": "23:00", "end": "06:00", "weekdays": false}`; 24-hour times, the car's own clock | `set_charging_schedule` (python-lucidmotors with that call; see below) |
| Keyless driving | `commands/keyless-driving` | `on`, `off`; the car turns it on only when locked with no key in reach | `KeylessDrivingActionRequest` (sent the way the library's own calls are; it has no method for it) |

Each command also takes `{"command": "on"}` (seat climate: `{"seat": "driver", "mode": "heat", "level": "low"}`). The state each one changes is published where the API has it, so a UI can show a toggle:

| state | attribute | from |
|---|---|---|
| Sentry on | `sentry` | `sentry_state.enablement_state` (enabled or idle) |
| Enhanced deterrence on | `enhanced_deterrence` | `sentry_state.enhanced_deterrence_state` (enabled or idle) |
| Alarm mode | `alarm` | `alarm.mode` |
| Windows | `windows/...` | `body.window_position` |
| Charge port door | `charge_port` | `body.charge_port` (not one of `doors`: it stands open for every charge) |
| Headlights | `lights/light_state` | `chassis.headlights` |
| Max A/C, creature comfort | `max_ac`, `creature_comfort` | `hvac.max_ac_status`, `hvac.keep_climate_status` (pet mode) |
| Seat levels | `seat_heat_driver`, `seat_vent_driver`, `seat_heat_front_passenger`, `seat_vent_front_passenger`, `seat_heat_rear_left`, `seat_heat_rear_center`, `seat_heat_rear_right` | `hvac.seats` (a seat's warmest zone) |
| Steering wheel heater | `steering_wheel_heater` | `hvac.steering_heater_level` |
| Battery preconditioning | `battery_preconditioning` | `battery.preconditioning_status` |
| AC current limit | `charging/settings/maximum_current` | `charging.energy_ac_current_limit` |
| Keyless driving | `keyless_driving` | `state.keyless_driving` |
| Charging schedule | `charging_schedule_on`, `_start`, `_end`, `_weekdays`, `_available` | the car's own schedule, read by its own request once an hour and again right after a write, never per poll |
| Defrost, rear window heat | `window_heating/heating_state`, `window_heating/windows/front`, `.../rear` | `hvac.defrost`, `hvac.rear_window_heating_status` (on if either is on, off only when both say off; the rear's "lost comm" states read unknown) |

Sentry at home and at work have no state in the API that the connector can read, so they are commands only.

From 0.4.0 a command the car's own config says it is not fitted for is refused before anything is sent: seat heat or ventilation on seats without it (`config.front_seats_heating`, `front_seats_ventilation`, `second_row_heated_seats`, and no rear centre seat on a six-seat car), the steering wheel heater on a car without one (`config.heated_steering_wheel`), and a remote frunk close on a gas strut (`config.frunk_strut`). Only an explicit "not fitted" refuses; a car that does not say keeps every control.

Left out on purpose: **software updates** (`apply_update`). The car cannot be driven while an update installs; that is a decision to make in the car or the app with the release notes in front of you, not a button on a dashboard. Also left out: individual seat zones (backrest apart from cushion: the car binds them in pairs and the app sets a seat at once), a full window drop (four windows fully down from afar is a theft and rain risk, and vent airs the cabin), the Sentry videos and pictures (their links point at Lucid's cloud; the event log itself is read as `sentry_events`), and opening or closing the charge port door, which on an Air opens by hand (its state is read; see below).

How they behave:

- **Only commands wake the car.** If the last poll saw the car asleep (any sleep or cloud power state, the same rule the poller reports), the command wakes it first, waits a few seconds, then sends. Polling still never wakes it.
- **Sent once.** Nothing is retried. A retried unlock or trunk is a second one if the first landed and only its answer was lost, so a failure comes back as an error and the decision is yours. A command not answered within 60 seconds, counting any login and wake it needed, is reported as such; check the car before sending it again. One that waited out the 60 seconds behind a poll was never sent, and says so.
- **Errors are short.** Rate limits, rejected sessions and refusals from the car all come back as a CarConnectivity `CommandError` naming the command and Lucid's reason.
- **Same session, same rate limit.** Commands use the poller's login and count against the same per-account allowance. A successful one triggers an early poll so the model shows the result.
- A climatization temperature in Fahrenheit is converted; anything outside 10 to 35 °C is refused as a probable unit mistake.

## What else it reads

From 0.5.0, all read-only and unknown (`None`) when the car leaves a field out or sends its zero. A figure whose unit the proto does not give is published unitless under a name ending `_raw`, and only while it means something.

| attribute | from | values |
|---|---|---|
| `battery_range_unavailable` | `battery.unavailable_range` | km the car is holding back (a cold pack) |
| `scheduled_charge`, `scheduled_charge_time_raw` | `charging.scheduled_charge`, `charge_scheduled_time` | `idle`, `scheduled`, `requested`: why a plugged-in car is not charging; the time as sent, only while scheduled |
| `charge_status` | `notifications.charging_general_status` | `normal`, `derated`, `preconditioning-saves-time` |
| `charge_session_range_raw`, `charge_port_power_raw` | `charging.charge_session_mi`, `port_power` | as sent, only while charging (the range's unit is unverified, as is whether the port power is the wall's) |
| `discharge_energy_raw` | `charging.discharge_energy` | as sent, only while discharging |
| `charge_session_current_limit` | `charging.active_session_ac_current_limit` | amps the charger allows; only with a cable in (an unplugged Gravity sends its own setting) |
| `sentry_alarm_sounding` | `sentry_state.remote_alarm_state` | true while the Sentry alarm is on |
| `sentry_range_cost_per_day_raw` | `sentry_state.range_cost_per_day` | the car's own figure, unit unknown, never 0 |
| `sentry_location_settings_raw` | `sentry_state.multiplex_values` | undecoded; probably the at-home and at-work settings |
| `sentry_events` | Sentry's event log | JSON text, oldest first: `[{"id", "at", "level", "type", "lat", "lon", "pictures"}]`, the last 30 days (100 at most), read once an hour. Pictures are counted, never linked or fetched |
| `tire_sensor_defective`, `tire_pressure_updated` | `chassis.sensor_defective_*`, `tire_pressure_last_updated` | a dead sensor told from a low tire; when the car last read the pressures, which also dates the pressures |
| `gear`, `drive_mode` | `gear_position`, `drive_mode` | `park`, `reverse`, `neutral`, `drive`; `comfort`, `swift`, `winter`, `valet`, `sport-plus`, `tow` and the rest (factory and developer modes read unknown) |
| `software_update_state`, `software_update_percent` | `software_update.state`, `percent_complete` | `installing`, `waiting`, `succeeded`, `failed` and more; the percent only while installing. The car's last result may stay after an install |
| `trim`, `paint_color` | `config.variant`, `paint_color` | the app's names, e.g. `Grand Touring`, `Zenith Red` |
| `ea_plan_name`, `ea_plan_expires` | `config.ea_subscription` | the Electrify America plan on the car and when it ends |
| `privacy_mode` | `state.privacy_mode` | true when the owner has switched the car's connectivity off |
| `mpb_fault` | `fault_state.mpb_fault_status` | `normal`, `warning`, `critical`, `hardware-fault`, for a module the proto does not name |
| `charging_records` | Lucid's charge detail records (`GetCdrs`) | JSON text, oldest first: id, start and stop (UTC), energy, times, charger name, currency; numbers as sent (no unit given); every six hours, only when the car has an account id. No address, coordinates or card |

`position/*` is dated from `gps.position_time` and the tire pressures from `tire_pressure_last_updated` when the car gives a time (seconds or milliseconds; told apart by size), so a stale reading is dated when it was taken. A time later than the poll is ignored.

A Gravity's release notes are now fetched too, with its own document type, when the installed python-lucidmotors has `get_document_info`.

Needs a python-lucidmotors that has `get_charging_schedule`, `set_charging_schedule` and `get_charging_data_records` (the release after 1.4.2). On an older one those attributes stay unknown and the schedule command refuses; everything else works as before.

The schedule's time format is not documented by Lucid. A write copies the format the car itself used in what it last sent (`HH:MM` when it holds none), and refuses, sending nothing, if it was in a form this does not know. `weekdays` is the proto's `weekday_on`, passed as given.

### Read in 0.4.0

From 0.4.0. The meanings come from the protobuf's field and enum names; one reading of a parked Gravity settled their zero and sentinel cases, and none has been seen while driving, charging or with an update waiting. Each one is unknown (`None`) when the car leaves the field out or sends 0 or a sentinel, never a guessed value.

| attribute | from | values |
|---|---|---|
| `charging/estimated_date_reached` | `charging.session_minutes_remaining` | when the charge reaches its limit; only while charging |
| `charging/connector/lock_state` | `charging.cable_lock` | locked, unlocked; only with a cable in |
| `charge_session_energy` | `charging.charge_session_kwh` | kWh added this session by the car's meter; only while charging, and never 0 |
| `alarm_status` | `alarm.status` | `disarmed`, `armed`, `pre-alarm`, `tilt`, `shock`, `intrusion`, `panic` |
| `sentry_inactive_reason` | `sentry_state.sentry_active_state.inactive_reason_*` | `low-battery`, `updating`, `occupied`, `at-home`, `at-work`, `usb-full`, `usb-error`, `tow-mode`, `camera-error`, `unattended-occupant`, `creature-comfort`; the first the car gives, and only when it gives one. `is_active` is not read: an armed, idle Gravity reports it false with no reason |
| `sentry_usb_drive_connected` | `sentry_state.usb_drive_status` | true, false; the car's "idle" reads unknown |
| `living_object_detection` | `body.living_object_detection_status` | `disabled`, `not-active`, `level-1` to `level-3` |
| `unattended_occupant_detection` | `body.unattended_occupant_detection_status` | `no-request`, `stage-1` to `stage-3`, `fault` |
| `keyfob_battery_low` | `body.keyfob_battery_status` | true, false |
| `gps_elevation_raw` | `gps.elevation` | the car's number as sent, unit unknown; None without a fix or when 0 (see below) |
| `battery_health_level` | `battery.battery_health_level` | the car's number as sent; probably a state of health in percent, but the proto does not say, so it is not named or scaled as one |
| `software_update_version` | `software_update.version_available` | the waiting version, e.g. `3.1.2`; only while `update_available` says AVAILABLE, as are the install time and the notes |
| `valet` | `state.drive_mode` | true in Valet mode, false in any other mode, unknown when the car does not say. Read only: nothing in the API sets it |
| `software_history` | OTA version history | every version the car has installed, oldest first, as JSON text: `[{"version": "3.1.51", "at": "2025-12-19T10:00:00Z"}]`, times in UTC. Read once, again when the car's version changes or after a day, never per poll. Lucid's build names are not kept |
| `software_update_install_time` | `software_update.install_duration_minutes` | how long it takes to install |
| `software_update_release_notes`, `software_update_release_notes_url` | `get_update_release_notes` | Lucid's notes for the waiting version and their link, fetched once per version (an hour later if the request fails), never per poll. Air only: the library can ask only for the Air's document, so a Gravity gets none rather than the wrong car's |

`gps_elevation_raw` is `gps.elevation` exactly as the car sends it: a plain number with no unit, None without a GPS fix. Its unit is unknown (a Gravity read 8932 where other cars at the same spot record 123 m), so it does not fill `position/altitude`; it is exposed so the value can be calibrated against known heights, for example drives logged beside other cars' elevations.

`charging.charge_session_mi`, the range added this session, may be miles as named or km as Lucid's other range figures are, so from 0.5.0 it is published as `charge_session_range_raw`, unitless.

`software_update_available` is true when the car announces an update and unknown otherwise: the proto has no value for "none waiting", so 0 no longer reads as false.

## Polling budget

What one account costs Lucid's servers, with the default config and one or more cars (a poll reads every car on the account in one call):

| Request | When | Per hour |
|---|---|---|
| Vehicle list (all state) | every `sleep_interval` (default `interval`, so unchanged) while every car is asleep; every `interval` (default and minimum 60 s) while one is awake; every `driving_interval` while one drives | 60 asleep by default (15 at `sleep_interval` 240); 60 awake; up to 240 driving at 15 s |
| Session refresh | when under 60 s of the 5-minute token remain, checked right before each read | about 15 by default; up to one per read with `sleep_interval` over about 150 s |
| OTA version history | once per car, again when its version changes, or daily; an hour after a failure | under 1 |
| Release notes | once per waiting version (Air only); an hour after a failure | under 1 |
| Commands | only when sent, plus one early poll 6 s later | as used |

Polls never wake the car; only commands do. A simulated day (a commute each way, two parked-awake stretches and two hours charging, a fake clock and API, `test/test_polling.py`) costs about 1,900 requests at the defaults and about 950 with `sleep_interval` 240. A command, or a write to the connector's `fast_poll` (below), puts a sleeping car back on the awake interval at once. A rate-limit refusal backs off from 5 s doubling to 15 minutes. Run one client per account: a second logger or integration on the same login shares this allowance.

## Session lifetime — read this if you run it long

`interval` is how often the car is polled, minimum 60 s. `driving_interval` is used instead while a car is driving or about to be, minimum 15 s, defaulting to `interval` so nothing changes unless you ask. A minute is plenty for a car on a driveway and too coarse for one moving: at 60 s a drive is a handful of points with straight lines between them. Driving is a small share of any day, so the extra requests are few.

Both are the spacing between polls, not the pause after one. A fetch of a sleeping car returns in about a tenth of a second, but Lucid has to reach a car that is awake and driving, and that fetch takes on the order of fifteen seconds — so waiting the full interval *after* it made a 15 s setting a 31 s cycle. The wait is the interval less however long the fetch took, and the fetch time is logged at debug level if you want to see it.

**Sleeping cars.** Lucid reports the car's power state; any sleep or cloud state (and not a charging car, whatever its power state says) counts as asleep. While every car on the account is asleep the poll waits `sleep_interval` (default `interval`, so nothing changes unless you set it; at most 900 s). A sleeping car cannot change unseen: its next change is a wake, and a wake shows on the first read after it, so the cost is that a wake can go unseen for up to `sleep_interval`. A drive that starts from sleep is noticed that late: its start time and its first stretch are missed. Leave it at the default if that matters. One car awake or driving keeps the whole account at its interval. Nothing here wakes the car.

**Asking for fast polling.** Writing a number of seconds (0 to 600) to the connector's `fast_poll` attribute reads at once and then holds the awake interval for that long even if the cars look asleep; 0 cancels it. Something that knows a car is about to be used, a garage door opening, can write it through any CarConnectivity plugin that sets a changeable attribute, as it would the charge limit (the attribute is `connectors/lucid/fast_poll`; not yet tried through the REST plugin). It sends nothing to the car, so it needs no `"commands": true`. A command also holds the awake interval for five minutes after it is sent, since the early poll may not yet see the wake.

**One account, one client.** Lucid rate-limits per account, not per connection, and it says nothing useful when it does: `RESOURCE_EXHAUSTED` with an empty body, no quota and no retry-after. Running a second logger against the same credentials — another CarConnectivity instance, a Home Assistant integration, a bridge of your own — competes for the same allowance, and the refusals arrive mid-drive when both are polling hardest. When one does arrive the connector waits 5 s, then 10, 20, 40 and so on to a 15-minute ceiling, and drops back to 5 s as soon as a poll succeeds.

A Lucid session's bearer token lasts **five minutes** and the server enforces that to the second. The connector refreshes only when under 60 s remain, never on every poll: the refresh endpoint is rate-limited, back-to-back refreshes return the same session anyway, and a client that refreshed per poll was throttled thousands of times before this was measured. The check happens right before each request, so the wait between polls does not matter to it: a token that has expired by the next poll is simply refreshed then. A 300 s token cannot span two polls more than about 150 s apart, so a sleeping poll is mostly a refresh and a read; a longer `sleep_interval` saves less than it looks, about half at 240 s.
