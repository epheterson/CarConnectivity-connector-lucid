# CarConnectivity-connector-lucid

A [CarConnectivity](https://github.com/tillsteinbach/CarConnectivity) connector for Lucid Motors vehicles (Air and Gravity), built on [python-lucidmotors](https://github.com/nshp/python-lucidmotors).

**Read-only.** It polls the same mobile API the Lucid app uses and populates the CarConnectivity model: state, odometer, battery level / range / capacity / cell temperatures, charging state and target, position, doors and locks, climatization, software version. Reads never wake the car, so polling costs nothing in battery drain. Commands are deliberately not implemented yet.

Verified against a 2026 Gravity on 2026-09-03.


## Installing

```
pip install "carconnectivity-connector-lucid @ git+https://github.com/epheterson/CarConnectivity-connector-lucid@v0.1.3"
```

Python 3.12 or newer. Not on PyPI yet: this package depends on `lucidmotors` at an unreleased upstream commit, for `login_with_refresh_token()`, and PyPI rejects direct-reference dependencies. The moment [nshp/python-lucidmotors](https://github.com/nshp/python-lucidmotors) cuts a release containing it, the dependency becomes an ordinary version specifier and a tag publishes to PyPI automatically.

## Install

```
pip install git+https://github.com/epheterson/CarConnectivity-connector-lucid.git
```

Until `python-lucidmotors` cuts a release that includes `login_with_refresh_token()` (merged, unreleased as of v1.1.9), this package pins that library to a git ref. That is deliberate: the refresh-token login is what lets the connector run with **no password on disk**.

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

## Session lifetime — read this if you run it long

`interval` is how often the car is polled, minimum 60 s. `driving_interval` is used instead while a car is driving or about to be, minimum 15 s, defaulting to `interval` so nothing changes unless you ask. A minute is plenty for a car on a driveway and too coarse for one moving: at 60 s a drive is a handful of points with straight lines between them. Driving is a small share of any day, so the extra requests are few.

A Lucid session's bearer token lasts **five minutes** and the server enforces that to the second. The connector refreshes only when under 60 s remain, never on every poll: the refresh endpoint is rate-limited, back-to-back refreshes return the same session anyway, and a client that refreshed per poll was throttled thousands of times before this was measured. Keep `interval` under 240 s or raise the refresh margin.
