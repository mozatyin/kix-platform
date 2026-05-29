"""SQLAlchemy ORM models package.

Originally a single ``app/models.py`` module — promoted to a package so
domain-specific models (subscription, etc.) can live in their own files
while keeping ``from app.models import X`` working for all existing
callers.

All ORM classes share the single ``Base`` declarative base defined in
``_core``. Submodules must import that same ``Base`` so Alembic's
``target_metadata`` sees the full table set.
"""

from __future__ import annotations

from app.models._core import (
    Base,
    BrandConfig,
    BrandLocation,
    EnergySnapshot,
    EnergyTransaction,
    ScoreArchive,
    UserProfile,
    VoucherPool,
)
from app.models.geofence import Geofence
from app.models.subscription import (
    BrandSubscription,
    SubscriptionHistory,
)

__all__ = [
    "Base",
    "BrandConfig",
    "BrandLocation",
    "BrandSubscription",
    "EnergySnapshot",
    "EnergyTransaction",
    "Geofence",
    "ScoreArchive",
    "SubscriptionHistory",
    "UserProfile",
    "VoucherPool",
]
