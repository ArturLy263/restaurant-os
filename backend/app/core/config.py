import uuid
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    database_url: str
    jwt_secret_key: str
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60
    admin_password: str

    # Outbox delivery worker (app.workers.outbox_worker); read only by its
    # runner, never by the API. Bounds reject unsafe values at startup.
    outbox_worker_batch_size: int = Field(default=100, ge=1, le=1000)
    outbox_worker_poll_interval_seconds: float = Field(default=1.0, gt=0, le=300)
    # Deliver only this restaurant's events; unset = every restaurant.
    outbox_worker_restaurant_id: uuid.UUID | None = None
    # Failed attempts after which an event is dead-lettered.
    outbox_max_attempts: int = Field(default=10, ge=1, le=100)
    outbox_publish_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    # Delivered events older than this are removed by `cleanup`.
    outbox_retention_days: int = Field(default=7, ge=1, le=3650)
    outbox_cleanup_batch_size: int = Field(default=1000, ge=1, le=10000)

    # Station event streams (WebSocket, app.realtime).
    # A new connection must authenticate within this time.
    websocket_auth_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    # How often an open connection's authorization is checked again
    # (user, role, station assignments); token expiry closes it at once.
    websocket_reauth_interval_seconds: float = Field(default=30.0, gt=0, le=3600)
    # Messages buffered per connection; a client that falls further
    # behind is disconnected (it reconnects and reloads the queue).
    websocket_queue_size: int = Field(default=100, ge=1, le=10000)
    # Open connections per API process.
    websocket_max_connections: int = Field(default=500, ge=1, le=100000)

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
