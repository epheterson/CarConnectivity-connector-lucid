"""The connector's own command types: what each accepts, what it hands its hook, and
that anything else is refused before a hook, and so the car, ever sees it."""

import pytest

from carconnectivity.commands import Commands
from carconnectivity.errors import SetterError

from carconnectivity_connectors.lucid.vehicle import ClimateLevel, LevelCommand, OnOffCommand, OpenCloseCommand, SeatClimateCommand, WindowsCommand


def _make(cls, name="x"):
    command = cls(name=name, parent=Commands(parent=None))
    command.enabled = True
    return command


def _hooked(command):
    seen = []

    def hook(_command, arguments):
        seen.append(arguments)
        return arguments

    command._add_on_set_hook(hook)
    return seen


@pytest.mark.parametrize(
    "cls,value,expected",
    [
        (OpenCloseCommand, "open", OpenCloseCommand.Command.OPEN),
        (OpenCloseCommand, {"command": "close"}, OpenCloseCommand.Command.CLOSE),
        (OnOffCommand, " on ", OnOffCommand.Command.ON),
        (OnOffCommand, OnOffCommand.Command.OFF, OnOffCommand.Command.OFF),
        (WindowsCommand, "vent", WindowsCommand.Command.VENT),
        (WindowsCommand, {"command": "close"}, WindowsCommand.Command.CLOSE),
        (LevelCommand, "medium", ClimateLevel.MEDIUM),
        (LevelCommand, {"command": "off"}, ClimateLevel.OFF),
    ],
)
def test_a_word_from_the_set_reaches_the_hook_as_the_enum(cls, value, expected):
    command = _make(cls)
    seen = _hooked(command)
    command.value = value
    assert seen == [{"command": expected}]


@pytest.mark.parametrize(
    "cls,value",
    [
        (OnOffCommand, "maybe"),
        (OnOffCommand, None),
        (OnOffCommand, 1),
        (WindowsCommand, "open"),
        (LevelCommand, "max"),
        (LevelCommand, 3),
        (OpenCloseCommand, {"command": "ajar"}),
    ],
)
def test_anything_else_is_refused_before_the_hook(cls, value):
    command = _make(cls)
    seen = _hooked(command)
    with pytest.raises(SetterError):
        command.value = value
    assert seen == []


@pytest.mark.parametrize(
    "value,seat,mode,level",
    [
        ("driver heat high", "driver", "heat", "high"),
        ("  front-passenger   vent  low ", "front-passenger", "vent", "low"),
        ({"seat": "rear-center", "mode": "heat", "level": "off"}, "rear-center", "heat", "off"),
    ],
)
def test_seat_climate_takes_a_seat_a_mode_and_a_level(value, seat, mode, level):
    command = _make(SeatClimateCommand, "seat-climate")
    seen = _hooked(command)
    command.value = value
    assert seen == [{"seat": SeatClimateCommand.Seat(seat), "mode": SeatClimateCommand.Mode(mode), "level": ClimateLevel(level)}]


@pytest.mark.parametrize(
    "value",
    [
        "driver heat",
        "driver heat high extra",
        "passenger heat low",
        "driver cool low",
        "driver heat 3",
        "rear-left vent low",
        {"seat": "driver", "mode": "heat"},
        {"command": "driver heat low"},
        None,
        5,
    ],
)
def test_seat_climate_refuses_anything_it_cannot_send(value):
    command = _make(SeatClimateCommand, "seat-climate")
    seen = _hooked(command)
    with pytest.raises(SetterError):
        command.value = value
    assert seen == []
