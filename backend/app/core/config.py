import uuid
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


BASE_DIR = Path(__file__).resolve().parents[3]


# Secrets the application refuses to start with (compared case-insensitively;
# a secret containing one of these is a copied example, not a secret).
PLACEHOLDER_MARKERS = (
    "change_me",
    "changeme",
    "change-me",
    "replace_me",
    "replace-me",
    "your_secret",
    "your-secret",
    "placeholder",
    "example",
)
# HS256 keys: at least this many characters (secrets.token_urlsafe(48)
# gives 64). Production needs the full 256 bits RFC 7518 asks for.
MIN_JWT_SECRET_LENGTH = 24
MIN_PRODUCTION_JWT_SECRET_LENGTH = 32
MIN_JWT_SECRET_DISTINCT_CHARACTERS = 10
# Database passwords a production deployment must never use.
WEAK_DATABASE_PASSWORDS = frozenset({"", "postgres", "password", "restaurant", "admin", "root"})
MIN_PRODUCTION_DATABASE_PASSWORD_LENGTH = 12


def looks_like_placeholder(value: str) -> bool:
    lowered = value.lower()
    return any(marker in lowered for marker in PLACEHOLDER_MARKERS)


class Settings(BaseSettings):
    # "production" turns on the strict checks below and hides the API docs.
    environment: Literal["development", "test", "production"] = "development"

    database_url: str
    jwt_secret_key: str = Field(repr=False)
    jwt_algorithm: Literal["HS256", "HS384", "HS512"] = "HS256"
    access_token_expire_minutes: int = Field(default=60, ge=1, le=24 * 60)

    # Login throttling (app.services.login_throttle), stored in PostgreSQL
    # so every API process shares it. Failures are counted per submitted
    # email and per client address within a window; past the limit the key
    # is blocked for a delay that doubles with each further failure, never
    # longer than login_block_max_seconds (no permanent lockout).
    login_throttle_enabled: bool = True
    login_max_failures_per_account: int = Field(default=5, ge=1, le=1000)
    login_max_failures_per_ip: int = Field(default=50, ge=1, le=100000)
    login_window_seconds: int = Field(default=900, ge=10, le=86400)
    login_block_base_seconds: int = Field(default=30, ge=1, le=3600)
    login_block_max_seconds: int = Field(default=900, ge=1, le=86400)

    # Interactive API docs (/docs, /redoc, /openapi.json); None means on
    # outside production and off in production.
    api_docs_enabled: bool | None = None

    # HTTP hardening (app.core.http_security).
    # Largest accepted request body; larger requests get 413.
    max_request_body_bytes: int = Field(default=1_048_576, ge=1024, le=104_857_600)
    # Strict-Transport-Security max-age; 0 sends no HSTS header. Only set it
    # when every client reaches the API over HTTPS (a TLS-terminating proxy).
    hsts_max_age_seconds: int = Field(default=0, ge=0, le=63_072_000)
    # Browser origins allowed to call the API cross-origin. Empty (the
    # default): same origin only, as the frontend is served behind one host.
    cors_allowed_origins: list[str] = Field(default_factory=list)

    # Outbox delivery worker (app.workers.outbox_worker); read by its
    # runner, and by the API only when OUTBOX_WEBSOCKET_DELIVERY is on.
    # Bounds reject unsafe values at startup.
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
    # Run the outbox worker inside the API process and deliver to its
    # streams (app.realtime.delivery). Off by default; enable it in one
    # API process only, and do not run the standalone worker's `run` then.
    outbox_websocket_delivery: bool = False
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
        # A configuration error must never print the configuration: it
        # holds the JWT secret and the database password.
        hide_input_in_errors=True,
    )

    @property
    def docs_enabled(self) -> bool:
        if self.api_docs_enabled is not None:
            return self.api_docs_enabled
        return self.environment != "production"

    @model_validator(mode="after")
    def _refuse_insecure_configuration(self) -> "Settings":
        """Fails startup on an insecure configuration. Messages name the
        setting, never its value."""

        secret = self.jwt_secret_key
        if not secret or not secret.strip():
            raise ValueError("JWT_SECRET_KEY is empty")
        if looks_like_placeholder(secret):
            raise ValueError("JWT_SECRET_KEY is a placeholder; generate one (see .env.example)")
        minimum = (
            MIN_PRODUCTION_JWT_SECRET_LENGTH if self.environment == "production" else MIN_JWT_SECRET_LENGTH
        )
        if len(secret) < minimum:
            raise ValueError(f"JWT_SECRET_KEY must have at least {minimum} characters")
        if len(set(secret)) < MIN_JWT_SECRET_DISTINCT_CHARACTERS:
            raise ValueError("JWT_SECRET_KEY is too repetitive to be a random secret")

        if self.login_block_base_seconds > self.login_block_max_seconds:
            raise ValueError("LOGIN_BLOCK_BASE_SECONDS must not exceed LOGIN_BLOCK_MAX_SECONDS")

        if self.environment == "production":
            try:
                password = make_url(self.database_url).password or ""
            except Exception as error:
                raise ValueError("DATABASE_URL is not a valid database URL") from error
            if (
                password.lower() in WEAK_DATABASE_PASSWORDS
                or looks_like_placeholder(password)
                or len(password) < MIN_PRODUCTION_DATABASE_PASSWORD_LENGTH
            ):
                raise ValueError(
                    "the password in DATABASE_URL is a placeholder or too weak for production "
                    f"(at least {MIN_PRODUCTION_DATABASE_PASSWORD_LENGTH} characters)"
                )
            if any(origin.strip() == "*" for origin in self.cors_allowed_origins):
                raise ValueError("CORS_ALLOWED_ORIGINS must list origins, not '*', in production")
        return self


settings = Settings()
