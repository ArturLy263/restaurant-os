import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class StationAssignment(Base):
    """A station whose preparations a Kitchen or Bar user may work.

    An assignment counts only if the station belongs to the user's
    restaurant, is active and has the station type of the user's role (see
    app.core.permissions.ROLE_STATION_TYPES); any other row grants nothing.
    """

    __tablename__ = "station_assignments"

    __table_args__ = (
        Index(
            "ix_station_assignments_station_id",
            "station_id",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"),
        primary_key=True,
    )

    station_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stations.id"),
        primary_key=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
