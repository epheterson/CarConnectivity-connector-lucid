# CarConnectivity-connector-lucid

[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/epheterson/CarConnectivity-connector-lucid/badge)](https://scorecard.dev/viewer/?uri=github.com/epheterson/CarConnectivity-connector-lucid)

A [CarConnectivity](https://github.com/tillsteinbach/CarConnectivity) connector for Lucid Motors vehicles (Air and Gravity), built on [python-lucidmotors](https://github.com/nshp/python-lucidmotors).

**Read-only by default.** It polls the same mobile API the Lucid app uses and populates the CarConnectivity model: state, odometer, battery level / range / capacity / cell temperatures, charging state and target, position, doors and locks, climatization, software version. Reads never wake the car, so polling costs nothing in battery drain. Commands (lock, climate, charging, trunk and the rest from 0.2.0; Halo Secure, windows, seats and more from 0.3.0) but stay off unless the config turns them on; see [Commands](#commands).

Verified against a 2026 Gravity on 2026-09-03.


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

Each command also takes `{"command": "on"}` (seat climate: `{"seat": "driver", "mode": "heat", "level": "low"}`). The state each one changes is published where the API has it, so a UI can show a toggle:

| state | attribute | from |
|---|---|---|
| Sentry on | `sentry` | `sentry_state.enablement_state` (enabled or idle) |
| Enhanced deterrence on | `enhanced_deterrence` | `sentry_state.enhanced_deterrence_state` (enabled or idle) |
| Alarm mode | `alarm` | `alarm.mode` |
| Windows | `windows/...` | `body.window_position` |
| Headlights | `lights/light_state` | `chassis.headlights` |
| Max A/C, creature comfort | `max_ac`, `creature_comfort` | `hvac.max_ac_status`, `hvac.keep_climate_status` (pet mode) |
| Seat levels | `seat_heat_driver`, `seat_vent_driver`, `seat_heat_front_passenger`, `seat_vent_front_passenger`, `seat_heat_rear_left`, `seat_heat_rear_center`, `seat_heat_rear_right` | `hvac.seats` (a seat's warmest zone) |
| Steering wheel heater | `steering_wheel_heater` | `hvac.steering_heater_level` |
| Battery preconditioning | `battery_preconditioning` | `battery.preconditioning_status` |
| AC current limit | `charging/settings/maximum_current` | `charging.energy_ac_current_limit` |

Sentry at home and at work have no state in the API that the connector can read, so they are commands only.

Left out on purpose: **software updates** (`apply_update`). The car cannot be driven while an update installs; that is a decision to make in the car or the app with the release notes in front of you, not a button on a dashboard. Also left out: individual seat zones (backrest apart from cushion: the car binds them in pairs and the app sets a seat at once), a full window drop (the library offers vent and close only), the sentry event and video history, which is a read API, not a command, and the charge port door: on an Air it opens by hand, and the car reports it open while it is shut.

How they behave:

- **Only commands wake the car.** If the last poll saw the car asleep (any sleep or cloud power state, the same rule the poller reports), the command wakes it first, waits a few seconds, then sends. Polling still never wakes it.
- **Sent once.** Nothing is retried. A retried unlock or trunk is a second one if the first landed and only its answer was lost, so a failure comes back as an error and the decision is yours. A command not answered within 60 seconds, counting any login and wake it needed, is reported as such; check the car before sending it again. One that waited out the 60 seconds behind a poll was never sent, and says so.
- **Errors are short.** Rate limits, rejected sessions and refusals from the car all come back as a CarConnectivity `CommandError` naming the command and Lucid's reason.
- **Same session, same rate limit.** Commands use the poller's login and count against the same per-account allowance. A successful one triggers an early poll so the model shows the result.
- A climatization temperature in Fahrenheit is converted; anything outside 10 to 35 °C is refused as a probable unit mistake.

## Session lifetime — read this if you run it long

`interval` is how often the car is polled, minimum 60 s. `driving_interval` is used instead while a car is driving or about to be, minimum 15 s, defaulting to `interval` so nothing changes unless you ask. A minute is plenty for a car on a driveway and too coarse for one moving: at 60 s a drive is a handful of points with straight lines between them. Driving is a small share of any day, so the extra requests are few.

Both are the spacing between polls, not the pause after one. A fetch of a sleeping car returns in about a tenth of a second, but Lucid has to reach a car that is awake and driving, and that fetch takes on the order of fifteen seconds — so waiting the full interval *after* it made a 15 s setting a 31 s cycle. The wait is the interval less however long the fetch took, and the fetch time is logged at debug level if you want to see it.

**One account, one client.** Lucid rate-limits per account, not per connection, and it says nothing useful when it does: `RESOURCE_EXHAUSTED` with an empty body, no quota and no retry-after. Running a second logger against the same credentials — another CarConnectivity instance, a Home Assistant integration, a bridge of your own — competes for the same allowance, and the refusals arrive mid-drive when both are polling hardest. When one does arrive the connector waits 5 s, then 10, 20, 40 and so on to a 15-minute ceiling, and drops back to 5 s as soon as a poll succeeds.

A Lucid session's bearer token lasts **five minutes** and the server enforces that to the second. The connector refreshes only when under 60 s remain, never on every poll: the refresh endpoint is rate-limited, back-to-back refreshes return the same session anyway, and a client that refreshed per poll was throttled thousands of times before this was measured. Keep `interval` under 240 s or raise the refresh margin.
