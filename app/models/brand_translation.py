"""Brand translation ORM model — i18n sidecar storage.

One row per ``(brand_id, field_name, locale)`` triple. The PK doubles
as the natural lookup key: a single primary-key hit fetches a localized
field value with no scan / no join.

Lifecycle
---------
``auto_translated`` flips to ``TRUE`` when the LLM produces the value;
``reviewed`` flips to ``TRUE`` only after a human (the brand owner OR
a platform admin) approves it. The two flags together drive the admin
review queue (see :func:`app.services.brand_translation_service.list_review_queue`).

The corresponding table is created by migration
``0004_i18n_brand_translations``; the GIN trigram index for admin
search lives in ``0006_i18n_collation``.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models._core import Base


class BrandTranslation(Base):
    """SQLAlchemy mapping for the ``brand_translations`` sidecar table."""

    __tablename__ = "brand_translations"

    # Composite primary key — every lookup uses all three columns.
    brand_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    field_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    locale: Mapped[str] = mapped_column(String(16), primary_key=True)

    value: Mapped[str] = mapped_column(Text, nullable=False)

    # ── Review-pipeline metadata ─────────────────────────────────────
    auto_translated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    reviewed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    reviewer_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )

    # ── Audit timestamps ─────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def to_dict(self) -> dict:
        """JSON-friendly projection for API responses."""
        return {
            "brand_id": self.brand_id,
            "field_name": self.field_name,
            "locale": self.locale,
            "value": self.value,
            "auto_translated": bool(self.auto_translated),
            "reviewed": bool(self.reviewed),
            "reviewer_id": self.reviewer_id,
            "created_at": (
                self.created_at.isoformat()
                if self.created_at is not None
                else None
            ),
            "updated_at": (
                self.updated_at.isoformat()
                if self.updated_at is not None
                else None
            ),
        }
