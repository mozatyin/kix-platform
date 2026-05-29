"""Geofence ORM model — PostGIS-backed spatial index.

Migrated from Redis ``GEOADD geofence:stores`` to PostgreSQL + PostGIS so
geofence storage scales beyond the single-ZSET ceiling. At 10K+ geofences
platform-wide, a single Redis sorted-set becomes the bottleneck:

* ``GEOSEARCH`` is ``O(N + log M)`` where N is the number of indexed
  members visited; a single hot ZSET serialises every brand's queries.
* PostGIS uses an R-tree GiST index, so ``ST_DWithin`` is ``O(log N)``
  with per-brand pre-filters and parallelisable across CPUs.

Geometry shape
--------------

* ``location`` — POINT (lng lat) in EPSG:4326, GEOGRAPHY type. Geography
  is preferred over geometry because ``ST_DWithin(geography, geography,
  meters)`` returns true ground-distance results without manual SRID
  reprojection.
* ``polygon`` — optional POLYGON for arbitrary-shape geofences (airport
  zones, malls, etc). Future use; nullable.

Dual-write
----------

Until backfill is verified, the existing Redis path in
``app/routers/geofence.py`` keeps writing to ``GEOADD geofence:stores``.
Writes now mirror to this PG table as well; reads continue to come from
Redis for now. After the 30-day soak, the read path will switch to
PostGIS via :func:`find_geofences_near` / :func:`is_inside_geofence`.
"""

from __future__ import annotations

from datetime import datetime

from geoalchemy2 import Geography
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models._core import Base


class Geofence(Base):
    """One row per merchant store / geofence region.

    The PK is the merchant-chosen ``store_id`` so dual-write keeps Redis
    and PG perfectly aligned without an extra translation table.
    """

    __tablename__ = "geofences"
    __table_args__ = (
        Index(
            "ix_geofence_location_gist",
            "location",
            postgresql_using="gist",
        ),
        Index("ix_geofence_brand_active", "brand_id", "active"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    brand_id: Mapped[str] = mapped_column(
        String(128), nullable=False, index=True
    )
    store_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(256), nullable=False)

    # POINT (lng lat) — GEOGRAPHY so distance queries return meters.
    location = mapped_column(
        Geography(geometry_type="POINT", srid=4326),
        nullable=False,
    )
    radius_meters: Mapped[int] = mapped_column(
        Integer, nullable=False, default=500
    )

    # Optional polygon for arbitrary shapes (future).
    polygon = mapped_column(
        Geography(geometry_type="POLYGON", srid=4326),
        nullable=True,
    )

    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    metadata_json: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # Audit columns mirror the rest of the schema for ops observability.
    db_created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    db_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def to_dict(self) -> dict:
        """Render as a Redis-compatible dict shape.

        Geography column is returned as a (lng, lat) pair so callers do
        not need to know about WKB / EWKT. ``polygon`` is intentionally
        omitted from this projection — callers that need it should fetch
        the ORM object directly.
        """
        # PostGIS Geography returns a WKBElement; the safest cross-driver
        # extraction is via the SQL layer (``ST_X`` / ``ST_Y``). When the
        # caller already has the ORM instance hydrated, we fall back to
        # parsing the WKT form. The ``location`` attribute is either a
        # ``WKBElement`` or, if just-constructed in Python, the WKT string
        # itself.
        lng: float | None = None
        lat: float | None = None
        loc = self.location
        if loc is not None:
            try:
                from geoalchemy2.shape import to_shape  # noqa: PLC0415

                pt = to_shape(loc)
                lng = float(pt.x)
                lat = float(pt.y)
            except (ImportError, AttributeError, ValueError, TypeError):
                # Last-ditch: parse a "POINT(lng lat)" WKT string
                if isinstance(loc, str) and loc.upper().startswith("POINT"):
                    try:
                        inside = loc[loc.index("(") + 1 : loc.index(")")]
                        parts = inside.split()
                        lng = float(parts[0])
                        lat = float(parts[1])
                    except (ValueError, IndexError):
                        pass

        return {
            "id": self.id,
            "brand_id": self.brand_id,
            "store_id": self.store_id,
            "name": self.name,
            "lat": lat,
            "lng": lng,
            "radius_meters": int(self.radius_meters or 0),
            "active": bool(self.active),
            "metadata": dict(self.metadata_json or {}),
            "created_at": int(self.created_at or 0),
            "updated_at": int(self.updated_at or 0),
        }
