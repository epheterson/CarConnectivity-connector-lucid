# Changelog

## Unreleased

### Added
- **Plug state.** `charging.connector.connection_state` and `charging.connector.external_power`, two slots the CarConnectivity model has always had and this connector left empty. Read off `ChargeState` rather than `body.charge_port`, which is the port's *door* and says nothing about a cable: 1 is `NOT_CONNECTED`, 0 is unknown like every other enum's zero, and every other value — `CABLE_CONNECTED`, the authorisation states, charging, complete, the faults, discharging — is one a car can only be in with a cable attached. External power is `ACTIVE` while drawing, `AVAILABLE` when plugged in and not, `UNAVAILABLE` with no cable. A "you left it unplugged" reminder downstream waits on exactly this.
- **Windows.** Four `windows.windows[...]` entries plus the summary `windows.open_state`, done the way the doors are: only windows that reported are summarised, so an absent body block is unknown rather than "all closed". Lucid's `WindowPositionStatus` is fifteen named positions, not a percentage; fully closed and the upper hard stop are closed, fully open and the lower hard stop are open, and everything between — the short and long drops, the vent position, the two in-between states, and the two motor positions (anti-trap back-off, the anti-clatter dip) — is ajar, because a window in any of those is not closed and that is what an alert cares about.

## 0.1.6 (2026-09-15)

### Fixed
- **`chassis.speed` is km/h, not metres per second, so every speed this connector published since v0.1.5 was 3.6x too high.** The protobuf annotates the field "in meters/second" and v0.1.5 believed it. The car disagrees: a 1.55-mile drive through a residential neighbourhood came out at 109, 171, 181 and 152 mph, on every moving fix rather than as an occasional glitch. Read as km/h the same fixes are 30, 47, 50 and 42, which is what that road looks like. A separate client that had logged the same car for a month read the field as km/h and its per-drive maxima matched two Teslas driven on the same roads. The proto comment is a reverse-engineered annotation, not Lucid's word. If you stored speeds from v0.1.5, divide them by 3.6.
- **`interval` and `driving_interval` are the spacing between polls, not the pause after one.** The loop waited the full interval after each fetch. A sleeping car answers in about a tenth of a second, so this was invisible at rest, but Lucid has to reach a car that is awake and moving and that fetch takes around fifteen seconds — making a 15 s `driving_interval` a 31 s cycle, exactly when the resolution matters. The wait is now the interval less the time the fetch took.
- **A rate-limit refusal no longer costs a quarter of an hour.** It waited a flat 900 seconds. Every refusal observed on one account arrived one to six minutes into a drive, so that silence swallowed the whole drive and most of the parking after it — a 1 m 20 s trip cost fifteen minutes of blindness. The wait now starts at 5 seconds and doubles — 5, 10, 20, 40, 80, 160, 320, 640 — to the same 900 second ceiling, and resets the moment a poll succeeds. Lucid gives no retry-after and an empty error body, so the wait was always a guess; a short guess costs one wasted call and a long one costs the drive.

### Changed
- Fetch duration is logged at debug level, so poll cadence can be read off the log instead of inferred from what was recorded downstream.
- README documents that Lucid rate-limits per account rather than per connection, and what running a second logger against the same credentials costs.

## 0.1.5 (2026-09-10)

### Added
- Speed. The car reports it in `chassis.speed` and CarConnectivity's model has nowhere to put it, so it hangs on the Lucid vehicle as a `connector_custom` `SpeedAttribute` in km/h rather than being discarded.
- `driving_interval`, a separate and shorter poll interval used while a car is driving or about to. Defaults to `interval`, so nothing changes unless it is set; minimum 15 seconds. At one poll a minute a drive is a handful of points with straight lines between them, and driving is a small share of any day.

## 0.1.4 (2026-09-07)

### Fixed
- **No drive this connector watched ever became a trip.** `transaction_end()` was called from `fetch_all()`, which runs once at startup, and not from `update_vehicles()`, which runs on every poll. Observers registered with `on_transaction_end=True` — the database plugin's trip agent among them — therefore heard the first fetch and nothing after it, for the life of the process. States, positions, charging and climate were all recorded normally throughout, which is what made it look like it was working. `fetch_vehicles()` now ends the transaction itself, after applying every vehicle, so a future caller cannot forget.

## 0.1.3 (2026-09-06)

### Fixed
- An unknown lock state is no longer reported as unlocked. Lucid's `LockState` 0 is UNKNOWN, but it fell through to UNLOCKED, so a car that had not yet said whether it was locked was published as unlocked — and anything watching for locked → unlocked raises an alarm about a car left open.
- Doors are only summarised when at least one of them reported. A missing body block used to publish "all doors closed", which is indistinguishable from six genuinely shut doors and is invented from no data.
- `requires-python` is `>=3.12`. It said `>=3.10` and the classifiers advertised 3.10 and 3.11, but the only dependency, `lucidmotors`, requires `^3.12`, so installing on 3.10 failed on the dependency (`Package 'lucidmotors' requires a different Python`) rather than being refused cleanly. Every release from v0.1.0 carried the wrong floor.
- The `lucidmotors` pin to upstream commit `13087ad` is in a tag for the first time. Up to and including v0.1.2 the dependency was `@main`, so two installs of the same tag could get different upstream code.

### Changed
- A tag publishes to PyPI through trusted publishing, once the dependency is one PyPI will accept. The workflow refuses rather than fails while it is a direct reference.
- CI tests Python 3.12 to 3.14, and runs on changes to `setup_requirements.txt` and the `Makefile` — a linter bump used to be merged without the workflow ever running.
- Dependabot is weekly and grouped. Daily and ungrouped opened six pull requests in one minute on publication day, which was enough noise to hide the one CI failure that mattered.
- Dev tooling: flake8 7.3, pylint 4.0.8, bandit 1.9.4. Flask is gone from `setup_requirements.txt`; this connector has no UI and never imported it.
- `project.urls` and keywords added.

### Tests
- 17 to 41. New coverage for the None-safe accessors, AC/DC charge typing, every enumerated and unenumerated charge state, lock and door states, position type, the metres-per-second speed conversion, and the temperature plausibility bounds. Both bugs above are covered by tests that were confirmed to fail before the fix.

## 0.1.2 (2026-09-04)
- Regression test for the battery capacity mapping.
- Silence pytimeparse's deprecation warning in the test run.

## 0.1.1 (2026-09-04)
- `available_capacity` comes from `capacity_kwhr`; the remaining-energy field (`kwhr`) is no longer reported as capacity. `total_capacity` is left unset because the API does not provide it.

## 0.1.0 (2026-09-03)
- First release. Read-only connector for Lucid Air and Gravity on CarConnectivity: state, odometer, battery level / range / capacity / cell temperatures, charging state and target level, position, doors and locks, climatization, software version. Refresh-token login only; the password is never stored. Reads never wake the car.
