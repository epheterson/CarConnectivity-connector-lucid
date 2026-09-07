"""Every poll has to end its transaction, or half the observers never hear anything.

CarConnectivity lets an observer register with `on_transaction_end=True`, meaning "tell me
once, when a whole update has landed" rather than once per attribute. `notify()` files
those flags away and `transaction_end()` delivers them. Nothing else does.

This connector called `transaction_end()` from `fetch_all()`, which runs once at startup,
and not from `update_vehicles()`, which runs on every poll. So those observers heard the
first fetch and then nothing, for as long as the process lived. The database plugin's trip
agent is one of them: it watches vehicle state to open a trip on ignition and close it on
park. No drive this connector ever watched became a trip — states and positions were
recorded the whole time, which is what made it look like it was working.
"""

import inspect

from carconnectivity_connectors.lucid.connector import Connector


def test_every_fetch_ends_its_transaction():
    """The delivery happens in fetch_vehicles, so a new caller cannot forget it."""
    body = inspect.getsource(Connector.fetch_vehicles)
    assert "transaction_end()" in body, "a poll that does not end its transaction delivers nothing to transaction-end observers"


def test_the_transaction_ends_after_the_vehicles_are_applied():
    """Ending it first would deliver the previous poll's flags and leave this one's queued —
    the same silence, one poll behind, which is harder to notice rather than easier."""
    body = inspect.getsource(Connector.fetch_vehicles)
    assert body.index("self._apply(") < body.index("transaction_end()")


def test_no_caller_ends_the_transaction_on_its_own():
    """Two places doing it is how it came to be done in only one of them."""
    for name in ("fetch_all", "update_vehicles"):
        body = inspect.getsource(getattr(Connector, name))
        assert "transaction_end" not in body, f"{name} ends the transaction itself; fetch_vehicles already does"


def test_both_entry_points_go_through_fetch_vehicles():
    """Whatever CarConnectivity calls — the first fetch or a later poll — has to reach the
    one place that delivers."""
    for name in ("fetch_all", "update_vehicles"):
        assert "self.fetch_vehicles()" in inspect.getsource(getattr(Connector, name)), name
