# Changelog

## 0.1.2 (2026-09-04)
- Regression test for the battery capacity mapping.
- Silence pytimeparse's deprecation warning in the test run.

## 0.1.1 (2026-09-04)
- `available_capacity` comes from `capacity_kwhr`; the remaining-energy field (`kwhr`) is no longer reported as capacity. `total_capacity` is left unset because the API does not provide it.

## 0.1.0 (2026-09-03)
- First release. Read-only connector for Lucid Air and Gravity on CarConnectivity: state, odometer, battery level / range / capacity / cell temperatures, charging state and target level, position, doors and locks, climatization, software version. Refresh-token login only; the password is never stored. Reads never wake the car.
