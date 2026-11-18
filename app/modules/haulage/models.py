"""Haulage, towing and SOS (FSD 3.6).

A customer books a tow; the fare is paid up front (held in escrow); nearby providers
are offered the job; the first to accept gets it. As elsewhere, money and status live on
the job's `Transaction`; the job row holds the route, the provider and live position.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from geoalchemy2 import Geography
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, Timestamps, UUIDPrimaryKey, enum_type


class ProviderLocation(Base):
    """Where a haulage provider (a truck or driver) is now and whether they take jobs.
    One row per provider; the app sends updates while the provider is online."""

    __tablename__ = "provider_locations"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    available: Mapped[bool] = mapped_column(Boolean, default=False)
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    location: Mapped[Any] = mapped_column(Geography("POINT", srid=4326))  # PostGIS, for dispatch
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class HaulageJob(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "haulage_jobs"

    transaction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("transactions.id", ondelete="RESTRICT"), unique=True
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    # Empty until a provider accepts.
    provider_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    is_sos: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    vehicle_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("vehicles.id", ondelete="SET NULL")
    )
    vehicle_description: Mapped[str] = mapped_column(String(200))
    # What is being towed; the fare depends on it (FR-6.5).
    vehicle_class: Mapped[str] = mapped_column(String(20), default="car", server_default="car")
    notes: Mapped[str | None] = mapped_column(String(500))

    pickup_address: Mapped[str] = mapped_column(String(300))
    pickup_lat: Mapped[float] = mapped_column(Float)
    pickup_lng: Mapped[float] = mapped_column(Float)
    pickup_location: Mapped[Any] = mapped_column(Geography("POINT", srid=4326))
    drop_address: Mapped[str] = mapped_column(String(300))
    drop_lat: Mapped[float] = mapped_column(Float)
    drop_lng: Mapped[float] = mapped_column(Float)
    distance_km: Mapped[float] = mapped_column(Float)
    fare_minor: Mapped[int] = mapped_column(BigInteger)

    # How many times dispatch has searched for a provider (the radius grows each round).
    dispatch_rounds: Mapped[int] = mapped_column(Integer, default=0)
    # Latest position reported by the assigned provider, for tracking.
    last_lat: Mapped[float | None] = mapped_column(Float)
    last_lng: Mapped[float | None] = mapped_column(Float)
    last_location_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    picked_up_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OfferStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    EXPIRED = "expired"  # timed out, or another provider took the job


class JobOffer(UUIDPrimaryKey, Timestamps, Base):
    """A job offered to one provider. Several providers get the same job; first to accept wins."""

    __tablename__ = "job_offers"

    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("haulage_jobs.id", ondelete="CASCADE"), index=True
    )
    provider_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    distance_m: Mapped[int] = mapped_column(Integer)  # provider -> pickup point
    status: Mapped[OfferStatus] = mapped_column(
        enum_type(OfferStatus), default=OfferStatus.PENDING, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
