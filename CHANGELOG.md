# Changelog

## 0.1.3 (2026-09-06)
- `requires-python` is `>=3.12`. It said `>=3.10`, but the only dependency, `lucidmotors`, requires `^3.12`, so installing on 3.10 or 3.11 failed on the dependency instead of being refused cleanly. The 3.10 and 3.11 classifiers are gone and CI tests 3.12-3.14.
- The `lucidmotors` pin to upstream commit `13087ad` is now in a tag. Up to and including v0.1.2 the dependency was `@main`, so two installs of the same tag could get different upstream code.

## 0.1.2 (2026-09-04)
- Regression test for the battery capacity mapping.
- Silence pytimeparse's deprecation warning in the test run.

## 0.1.1 (2026-09-04)
- `available_capacity` comes from `capacity_kwhr`; the remaining-energy field (`kwhr`) is no longer reported as capacity. `total_capacity` is left unset because the API does not provide it.

## 0.1.0 (2026-09-03)
- First release. Read-only connector for Lucid Air and Gravity on CarConnectivity: state, odometer, battery level / range / capacity / cell temperatures, charging state and target level, position, doors and locks, climatization, software version. Refresh-token login only; the password is never stored. Reads never wake the car.
