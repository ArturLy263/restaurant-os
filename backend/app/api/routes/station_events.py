import uuid

from fastapi import APIRouter, WebSocket

from app.realtime.stream import StationEventStream


router = APIRouter(
    prefix="/api/v1/stations",
    tags=["kitchen"],
)


@router.websocket("/{station_id}/events")
async def station_events(websocket: WebSocket, station_id: uuid.UUID):
    """Live events of one station; protocol in app.realtime.stream."""

    await StationEventStream(websocket, station_id).run()
