# CarConnectivity-connector-lucid — plan

Tier-1: checklist before code, commit per unit, every stage has a check that can fail.

Read-only first. Their own Volvo connector shipped read-only; that is the precedent. Writes come after the command-policy layer exists (see `~/vault/docs/plans/2026-09-03-lucid-auth-and-writes.md`).

## Stage map

| # | stage | output | failable check |
|---|---|---|---|
| ✅ 1 | Skeleton mirrored from the Volvo connector | pyproject, LICENSE, CI, Makefile, namespace package | `pip install -e .` succeeds; `carconnectivity` discovers `carconnectivity_connectors.lucid` |
| ✅ 2 | `auth/session.py` — refresh-token file → `LucidAPI`; refresh at `< 60 s`; private asyncio loop owned by the connector thread | `LucidSession.api()` returns an authenticated client | unit test with a mocked `LucidAPI`: refresh called once per 5-min window, not per poll; `RESOURCE_EXHAUSTED` → `TooManyRequestsError`; `UNAUTHENTICATED` → `TemporaryAuthenticationError` |
| ✅ 3 | `vehicle.py` — `LucidVehicle(GenericVehicle)`, `LucidElectricVehicle(ElectricVehicle, LucidVehicle)` | classes | instantiate against a `Garage` without a live API |
| ✅ 4 | `mapping.py` — pure functions: Lucid protobuf → CarConnectivity attributes (units km/°C/bar, enum ints → `ChargingState`, `LockState`, `OpenState`, `PositionType`, `GenericVehicle.State`; power states 12/13 = not awake) | functions with no I/O | unit tests pinned to the real 2026-08-10 snapshot in `~/.zosia/vehicles.db` (SoC 50.7, range 315 km, odo 191.9 km, pack 117.33 kWh) |
| ✅ 5 | `connector.py` — `Connector(BaseConnector)`: config, background thread, `fetch_all` → `fetch_vehicles` → `update_vehicles`; error taxonomy; `get_type()` = `carconnectivity-connector-lucid` | the connector | integration run against the real car via `carconnectivity-cli` with the JSON in `test/integration_test/`; garage shows TARS with SoC/range/odometer/position/doors/charging populated |
| ✅ 6 | `doc/Config.md`, README (config, the 5-minute-token note, the git-ref pin) | docs | a stranger can configure it from the README alone |
| 7 | Vault: registry entry when it runs as a service; `home/vehicles.md` pointer | — | entry exists in the same commit that adds the service |

## Not in scope (yet)

Commands. Images. Trip/charge history (that is EVify's history engine, not a connector concern).

## Decisions

- **Refresh token lives in a file, path in config** (`refresh_token_file`), never in the JSON config itself and never in env. Matches the bridge and keeps `config_remove_credentials()` honest.
- **One private event loop per connector**, created in `startup()` on the background thread. `lucidmotors` is asyncio; CarConnectivity connectors are threads. Same pattern as the bridge.
- **Interval floor 60 s** (their minimum). Reads never wake a Lucid, so polling is free of vampire drain.
- **`vehicle.state`** derived like the bridge's `is_asleep()`: charging overrides sleep; 12/13 treated as not-awake.

## Evidence — 2026-09-03

- Stage 1: `pip install -e .` OK; `carconnectivity` discovery finds `carconnectivity_connectors.lucid`, `get_type()` = `carconnectivity-connector-lucid`.
- Stages 2–4: 12 tests pass, flake8 clean. Cadence test: ten polls, zero refreshes inside the window.
- Stage 5: real car via `CarConnectivity(config)`: TARS / Gravity / PARKED / REACHABLE; level 77 %, 480 km; battery 117.21 / 90.30 kWh, cells 21.8–22.7 °C; PARKING + heading; six doors + lock; climatization + target; charging target 90 %; software 3.6.4. `_apply` in-thread: no exception. Second TTL reading right after login: 300 s.
- Not done: stage 7 (registry entry) — only applies once it runs as a service.

## Evidence — 2026-09-03 evening (Car Command SP1)
- Tagged v0.1.0 (a5178d2) and shipped as a wheel inside the `carcommand-cc` image on the Mini (CarConnectivity 0.11.11 + database/webui/restapi plugins). Live: connector healthy, connection_state connected, TARS in the garage, WebUI in miles (locale en_US.UTF-8).
- v0.1.1 (f622e0e): `available_capacity` now comes from `capacity_kwhr`; `kwhr` (energy remaining, tracks SoC) is no longer reported as capacity; `total_capacity` left unset (the car does not report gross). Found because `drives.capacity` read 87.5 of 117.2 at ~75% SoC. Regression test `test/test_apply_drive.py` (630fe10); suite 17 passed, no warnings.
- Stack, dashboards and registry: `~/vault/docs/plans/2026-09-03-carcommand-sp1-plan.md`.

## Stage 8 — the fields the car sends and this connector throws away (surveyed 2026-09-15)

Probed against the live car, not read off the proto: every value below came back populated on a parked Gravity. The connector publishes about twenty-five attributes; `VehicleState` carries twenty-nine top-level blocks.

| Lucid field | live value | CarConnectivity slot | why it matters |
|---|---|---|---|
| `body.charge_port` | `1` | `charging.connector` | the model has the slot and it is empty. Unblocks a plug-in reminder — "you left it unplugged" — which needs nothing but this |
| `body.window_position` | per-window enum, all `FULLY_CLOSED` | `windows.windows[].open_state` | slot exists and is empty. A windows-left-down alert |
| `cabin.interior_temp` | `23.4` °C | none — `connector_custom` | cabin temperature. Only Teslas have it downstream today, via TeslaFi |
| `chassis.*_tire_pressure_bar` + four hard/soft warnings | `2.85` bar (41.3 psi) | none — see tillsteinbach/CarConnectivity#172 | TPMS. The TeslaFi export has no tire field at all, so for this fleet the Lucid is the *only* car that can ever report it |
| `battery.capacity_kwhr`, `battery.kwhr` | `117.15`, `92.63` | `drives` / custom | usable pack capacity measured rather than assumed from the model name |
| `battery.battery_health`, `max_cell_temp`, `min_cell_temp` | `1`, `24.4`, `22.6` °C | custom | battery page depth; cell spread is the number that says a pack is unwell |
| `sentry_state` | `SENTRY_STATE_IDLE` (+ threat level, USB status) | custom | Sentry alerts, and the reason the parked-cost table downstream shows "not enough yet" for this car: nothing records when Sentry is on |
| `alarm.status`, `alarm.mode` | `2`, `2` | pending #172 | tow vs intrusion vs panic, which `TRIGGERED` alone cannot say |
| `chassis.headlights`, `hazard_lights` | `3`, `0` | `lights` | slot exists and is empty |
| `software_update.update_available`, `version_available` | `0`, `0.0.0` | `software` | an update-available notification |
| `gear_position`, `drive_mode`, `low_power_mode_status` | `1`, `1`, `2` | `state` refinement | drive detection currently leans on `power` alone |

**Order to do them in**, by value over effort: `charging.connector` and `windows` first — both are empty slots in the model, so they need mapping and nothing else. Then `cabin.interior_temp` and `sentry_state` as `connector_custom`, the way `speed` already is. Tire pressures and `alarm` wait on #172, because implementing them before the framework decides its shape means implementing them twice.

**Anticipate more.** This survey found eleven usable blocks in an afternoon, which says the ratio of published to available is roughly two in five, and the remaining blocks (`tcu`, `fault_state`, `notifications`, `trailer_state`, `privacy_mode`) have not been looked at. Two lessons from the 0.1.6 fixes apply to all of it: the proto's comments are reverse-engineered annotations and not Lucid's word, so every unit gets checked against a value the car produces; and a wrong unit still looks like a plausible reading, so "it renders" is not a check.
