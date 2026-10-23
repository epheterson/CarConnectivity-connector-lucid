"""The waiting update and its release notes, through a connector on a fake API that hands
back real protobuf messages: one request per version, none per poll, none without a
version, and a failure that neither breaks the poll nor retries on the next one."""

import asyncio
import json
import time
from datetime import timedelta

import pytest

from carconnectivity.carconnectivity import CarConnectivity
from carconnectivity.errors import TooManyRequestsError
from lucidmotors.gen import vehicle_state_service_pb2 as pb

from carconnectivity_connectors.lucid import connector as connector_module
from carconnectivity_connectors.lucid.auth.session import LucidSession
from carconnectivity_connectors.lucid.connector import Connector

VIN = "LUCIDTESTVIN00042"


class FakeAPI:
    def __init__(self):
        self.software_update = pb.SoftwareUpdate()
        self.notes = pb.GetDocumentInfoResponse(
            url="https://example.invalid/notes/3.1.2", info=pb.DocumentInfo(version="3.1.2", description="Faster charging curve.")
        )
        self.fail_with = None
        self.hang = False
        self.asked = []
        self.model = pb.MODEL_AIR
        self.polls = 0

    async def __aenter__(self):
        return self

    async def login_with_refresh_token(self, token):
        pass

    @property
    def session_time_remaining(self):
        return timedelta(seconds=300)

    async def fetch_vehicles(self):
        self.polls += 1  # each poll is a later reading, as on a car; CarConnectivity drops one that is not
        state = pb.VehicleState(last_updated_ms=1_700_000_000_000 + self.polls * 60_000, power=pb.POWER_STATE_SLEEP, software_update=self.software_update)
        return [pb.Vehicle(vehicle_id="vid-1", config=pb.VehicleConfig(vin=VIN, nickname="Bravo", model=self.model), state=state)]

    async def get_update_release_notes(self, version, vehicle=None):
        self.asked.append((version, vehicle.vehicle_id))
        if self.hang:
            await asyncio.sleep(10)
        if self.fail_with is not None:
            raise self.fail_with
        return self.notes

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


def waiting(version="3.1.2", minutes=25):
    return pb.SoftwareUpdate(update_available=pb.UPDATE_AVAILABLE, version_available=version, install_duration_minutes=minutes)


def test_nothing_waiting_asks_for_nothing(rig):
    connector, api, cc = rig
    connector.fetch_vehicles()
    vehicle = cc.garage.get_vehicle(VIN)
    assert api.asked == []
    assert vehicle.software_update_version.value is None and vehicle.software_update_install_time.value is None
    assert vehicle.software_update_release_notes.value is None and vehicle.software_update_release_notes_url.value is None


def test_notes_for_another_version_are_a_failed_read_tried_again_later(rig):
    connector, api, cc = rig
    api.software_update = waiting("3.2.0")
    api.notes = pb.GetDocumentInfoResponse(url="https://example.invalid/notes/3.1.2", info=pb.DocumentInfo(version="3.1.2", description="Faster charging curve."))
    connector.fetch_vehicles()
    vehicle = cc.garage.get_vehicle(VIN)
    assert vehicle.software_update_release_notes.value is None and vehicle.software_update_release_notes_url.value is None, "3.1.2's notes are not 3.2.0's"
    connector.fetch_vehicles()
    assert len(api.asked) == 1, "a mismatch waits out the retry like any failed read"


def test_the_waiting_version_and_its_notes_once_per_version(rig):
    connector, api, cc = rig
    api.software_update = waiting()
    for _ in range(3):
        connector.fetch_vehicles()
    vehicle = cc.garage.get_vehicle(VIN)
    assert api.asked == [("3.1.2", "vid-1")], "one request for three polls"
    assert vehicle.software_update_version.value == "3.1.2"
    assert vehicle.software_update_install_time.value == timedelta(minutes=25)
    assert vehicle.software_update_release_notes.value == "Faster charging curve."
    assert vehicle.software_update_release_notes_url.value == "https://example.invalid/notes/3.1.2"

    api.software_update = waiting("3.2.0")
    api.notes = pb.GetDocumentInfoResponse()  # an empty reply
    connector.fetch_vehicles()
    assert api.asked[-1] == ("3.2.0", "vid-1")
    assert vehicle.software_update_release_notes.value is None, "the old version's notes are not this one's"

    api.software_update = pb.SoftwareUpdate()  # installed
    connector.fetch_vehicles()
    assert vehicle.software_update_version.value is None and vehicle.software_update_release_notes_url.value is None
    assert len(api.asked) == 2


@pytest.mark.parametrize("minutes", [0, 65535])
def test_an_unset_or_sentinel_install_time_is_unknown(rig, minutes):
    connector, api, cc = rig
    api.software_update = waiting(minutes=minutes)
    connector.fetch_vehicles()
    assert cc.garage.get_vehicle(VIN).software_update_install_time.value is None


def test_a_failed_request_does_not_break_the_poll_or_repeat_on_the_next(rig, monkeypatch):
    connector, api, cc = rig
    api.software_update = waiting()
    api.fail_with = TooManyRequestsError("throttled")
    connector.fetch_vehicles()
    connector.fetch_vehicles()
    vehicle = cc.garage.get_vehicle(VIN)
    assert len(api.asked) == 1, "the retry waits an hour, not a poll"
    assert vehicle.software_update_version.value == "3.1.2" and vehicle.software_update_release_notes.value is None

    api.fail_with = None
    now = connector_module.time.monotonic()
    monkeypatch.setattr(connector_module.time, "monotonic", lambda: now + connector_module.RELEASE_NOTES_RETRY_S + 1)
    connector.fetch_vehicles()
    assert len(api.asked) == 2 and vehicle.software_update_release_notes.value == "Faster charging curve."


@pytest.mark.parametrize("model", [pb.MODEL_GRAVITY, pb.MODEL_UNKNOWN])
def test_a_car_not_known_to_be_an_air_gets_no_notes(rig, model):
    """The library can only ask for the Air's document; on a Gravity it would be the wrong car's notes."""
    connector, api, cc = rig
    api.model = model
    api.software_update = waiting()
    connector.fetch_vehicles()
    vehicle = cc.garage.get_vehicle(VIN)
    assert api.asked == []
    assert vehicle.software_update_release_notes.value is None and vehicle.software_update_release_notes_url.value is None
    assert vehicle.software_update_version.value == "3.1.2", "the version is still the car's own word"


@pytest.mark.parametrize("flag", [pb.UPDATE_AVAILABILITY_UNKNOWN])
def test_a_version_without_the_available_flag_is_not_waiting(rig, flag):
    """version_available may name the version just installed; only AVAILABLE makes it waiting."""
    connector, api, cc = rig
    api.software_update = pb.SoftwareUpdate(update_available=flag, version_available="3.1.2", install_duration_minutes=25)
    connector.fetch_vehicles()
    vehicle = cc.garage.get_vehicle(VIN)
    assert api.asked == []
    assert vehicle.software_update_version.value is None and vehicle.software_update_install_time.value is None
    assert vehicle.software_update_release_notes.value is None and vehicle.software_update_available.value is None


def test_a_failure_holds_back_only_its_own_version(rig):
    connector, api, cc = rig
    api.software_update = waiting("3.1.2")
    api.fail_with = TooManyRequestsError("throttled")
    connector.fetch_vehicles()
    api.fail_with = None
    api.software_update = waiting("3.2.0")
    api.notes = pb.GetDocumentInfoResponse(url="https://example.invalid/notes/3.2.0", info=pb.DocumentInfo(version="3.2.0", description="Faster charging curve."))
    connector.fetch_vehicles()
    assert [v for v, _ in api.asked] == ["3.1.2", "3.2.0"], "a new version is asked for at once"
    assert cc.garage.get_vehicle(VIN).software_update_release_notes.value == "Faster charging curve."


def test_a_reply_that_never_comes_gives_up_and_the_poll_goes_on(rig, monkeypatch):
    connector, api, cc = rig
    monkeypatch.setattr("carconnectivity_connectors.lucid.auth.session.RELEASE_NOTES_TIMEOUT_S", 0.2)
    api.software_update = waiting()
    api.hang = True
    started = time.monotonic()
    connector.fetch_vehicles()
    assert time.monotonic() - started < 5, "the poll was held for the whole hang"
    vehicle = cc.garage.get_vehicle(VIN)
    assert vehicle.software_update_version.value == "3.1.2" and vehicle.software_update_release_notes.value is None
