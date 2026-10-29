"""A status change the central rules do not allow fails the request and
changes nothing, even when an operation's own guard lets it through."""

from fastapi.testclient import TestClient

from app.main import app
from app.services import order_service
from app.services.outbox_service import OutboxService


def test_a_change_past_a_wrong_guard_fails_and_rolls_back(pos, monkeypatch):
    order_id, burger_id, _ = pos.sent_order()
    preparation = pos.preparation(burger_id)
    events_before = len(pos.events("preparation.ready"))

    # A broken guard that would let "ready" skip IN_PROGRESS. The event
    # contract would refuse the change too (building its payload fails);
    # take it out so that only the state rules stand in the way.
    monkeypatch.setattr(order_service, "PREPARATION_STEPS", {"PENDING": "READY"})
    monkeypatch.setattr(order_service, "PreparationStatusChangedV1", lambda **fields: None)
    monkeypatch.setattr(OutboxService, "record", staticmethod(lambda *args, **kwargs: None))
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(f"/api/v1/preparations/{preparation.id}/ready", headers=pos.headers)

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert pos.preparation(burger_id).status == "PENDING"
    assert pos.preparation(burger_id).ready_at is None
    assert len(pos.events("preparation.ready")) == events_before
