"""The OTA history: published as version and time only (never Lucid's build names, which carry
other cars' VINs), repeats collapsed, read once and again only when the car's version stops
matching it, a failure neither breaking the poll nor retried on the next one."""

import json
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest

from carconnectivity.carconnectivity import CarConnectivity
from lucidmotors.gen import vehicle_state_service_pb2 as pb

from carconnectivity_connectors.lucid import mapping
from carconnectivity_connectors.lucid.auth.session import LucidSession
from carconnectivity_connectors.lucid.connector import Connector

VIN = "LUCIDTESTVIN00042"
SECRET = "BUILD-FOR-LUCIDOTHERVIN0001"


def entry(version, stamp):
    return SimpleNamespace(version=version, name=SECRET, updateTime=stamp)


def test_oldest_first_utc_and_no_name():
    out = json.loads(mapping.ota_history([entry("3.2.0", "2026-03-01T08:30:00.123456"), entry("3.1.1", "2025-12-19T20:42:10")]))
    assert out == [{"version": "3.1.1", "at": "2025-12-19T20:42:10Z"}, {"version": "3.2.0", "at": "2026-03-01T08:30:00Z"}]
    assert SECRET not in mapping.ota_history([entry("3.1.1", "2025-12-19T20:42:10")])


def test_a_version_listed_again_within_a_day_keeps_the_first():
    rows = [entry("3.3.20", "2025-12-20T10:07:00"), entry("3.3.20", "2025-12-20T10:00:00"), entry("3.4.0", "2026-01-05T00:00:00")]
    assert [r["version"] for r in json.loads(mapping.ota_history(rows))] == ["3.3.20", "3.4.0"]
    assert json.loads(mapping.ota_history(rows))[0]["at"] == "2025-12-20T10:00:00Z"


def test_a_real_return_to_an_older_version_stays():
    rows = [entry("3.1.1", "2025-12-01T00:00:00"), entry("3.2.0", "2026-01-01T00:00:00"), entry("3.1.1", "2026-02-01T00:00:00")]
    assert [r["version"] for r in json.loads(mapping.ota_history(rows))] == ["3.1.1", "3.2.0", "3.1.1"]


@pytest.mark.parametrize("values", [None, [], [entry("", "2025-12-19T20:42:10")], [entry("3.1.1", "")], [entry("3.1.1", "not a time")]])
def test_nothing_usable_is_none(values):
    assert mapping.ota_history(values) is None


class Service:
    def __init__(self, api):
        self.api = api

    async def GetOtaVersionHistory(self, request):  # pylint: disable=invalid-name
        self.api.asked.append((request.vin, request.offset, request.limit))
        if self.api.fail_with is not None:
            raise self.api.fail_with
        values = [pb.OtaVersionHistoryValue(version=v, name=SECRET, updateTime=t) for v, t in self.api.values]
        return pb.GetOtaVersionHistoryResponse(values=values)


class FakeAPI:
    def __init__(self):
        self.values = [("3.2.0", "2026-03-01T08:30:00"), ("3.1.1", "2025-12-19T20:42:10")]
        self.software = "3.2.0"
        self.fail_with = None
        self.asked = []
        self.polls = 0
        self._vehicle_service = Service(self)

    async def __aenter__(self):
        return self

    async def login_with_refresh_token(self, token):
        pass

    @property
    def session_time_remaining(self):
        return timedelta(seconds=300)

    async def fetch_vehicles(self):
        self.polls += 1
        state = pb.VehicleState(last_updated_ms=1_700_000_000_000 + self.polls * 60_000, power=pb.POWER_STATE_SLEEP,
                                chassis=pb.ChassisState(software_version=self.software))
        return [pb.Vehicle(vehicle_id="vid-1", config=pb.VehicleConfig(vin=VIN, nickname="Bravo", model=pb.MODEL_GRAVITY), state=state)]

    async def close(self):
        pass


@pytest.fixture(autouse=True)
def _no_ntp(monkeypatch):
    monkeypatch.setattr("carconnectivity.carconnectivity.ntp_time_delta", lambda: None)


@pytest.fixture
def rig(tmp_path):
    token = tmp_path / "token.json"
    token.write_text(json.dumps({"refresh_token": "opaque-test-token"}))
    cc = CarConnectivity(config={"carConnectivity": {}}, tokenstore_file=str(tmp_path / "tokens"), cache_file=str(tmp_path / "cache"))
    connector = Connector("lucid", cc, {"refresh_token_file": str(token)})
    api = FakeAPI()
    connector._session = LucidSession(token, api_factory=lambda: api)
    connector._session.start()
    try:
        yield connector, api, cc
    finally:
        connector._session.close()
        cc.shutdown()


def published(cc):
    return json.loads(cc.garage.get_vehicle(VIN).software_history.value)


def test_read_once_for_many_polls(rig):
    connector, api, cc = rig
    for _ in range(4):
        connector.fetch_vehicles()
    assert api.asked == [(VIN, 0, 100)]
    assert [r["version"] for r in published(cc)] == ["3.1.1", "3.2.0"]
    assert SECRET not in cc.garage.get_vehicle(VIN).software_history.value


def test_a_new_car_version_reads_again_and_a_stale_history_retries_in_an_hour(rig):
    connector, api, cc = rig
    connector.fetch_vehicles()
    api.software = "3.3.0"  # installed; the history does not list it yet
    connector.fetch_vehicles()
    assert len(api.asked) == 2
    connector.fetch_vehicles()
    assert len(api.asked) == 2, "the history is behind the car, so it waits an hour"
    api.values = [("3.3.0", "2026-04-01T00:00:00")] + api.values
    history, _, current = connector._ota_history[VIN]
    connector._ota_history[VIN] = (history, time.monotonic() - 1, current)
    connector.fetch_vehicles()
    assert [r["version"] for r in published(cc)] == ["3.1.1", "3.2.0", "3.3.0"]


def test_a_failure_keeps_the_poll_and_the_last_history(rig):
    connector, api, cc = rig
    api.fail_with = RuntimeError("down")
    connector.fetch_vehicles()
    connector.fetch_vehicles()
    vehicle = cc.garage.get_vehicle(VIN)
    assert vehicle.software_history.value is None and len(api.asked) == 1
    api.fail_with = None
    history, _, current = connector._ota_history[VIN]
    connector._ota_history[VIN] = (history, time.monotonic() - 1, current)
    connector.fetch_vehicles()
    assert [r["version"] for r in published(cc)] == ["3.1.1", "3.2.0"]
    api.fail_with = RuntimeError("down again")
    connector._ota_history[VIN] = (connector._ota_history[VIN][0], time.monotonic() - 1, "3.2.0")
    connector.fetch_vehicles()
    assert len(published(cc)) == 2, "a failed re-read keeps what was read"
