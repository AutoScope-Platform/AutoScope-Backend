"""Catalog tables: the owner's garage and the marketplace listings (FSD 3.2-3.4).

Four things can be listed, all in one `listings` table so search covers them together:
vehicles for sale, parts, mechanic services and insurance products. Fields that only
make sense for one type (make/model/year, part number, cover type...) live in the
`attributes` JSON column, checked per type in `service.py`.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from geoalchemy2 import Geography
from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, Timestamps, UUIDPrimaryKey, enum_type, ref_column


# --- Garage ---------------------------------------------------------------------------
class Vehicle(UUIDPrimaryKey, Timestamps, Base):
    """A vehicle in an owner's garage. Its service history is a list of `ServiceRecord`."""

    __tablename__ = "vehicles"

    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    vin: Mapped[str | None] = mapped_column(String(17), index=True)
    make: Mapped[str] = mapped_column(String(60))
    model: Mapped[str] = mapped_column(String(60))
    year: Mapped[int] = mapped_column(Integer)
    plate_number: Mapped[str | None] = mapped_column(String(20))
    colour: Mapped[str | None] = mapped_column(String(30))
    mileage_km: Mapped[int | None] = mapped_column(Integer)
    fuel_type: Mapped[str | None] = mapped_column(String(30))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    service_records: Mapped[list[ServiceRecord]] = relationship(
        back_populates="vehicle",
        cascade="all, delete-orphan",
        order_by="ServiceRecord.done_on.desc()",
    )


class ServiceRecord(UUIDPrimaryKey, Timestamps, Base):
    """One maintenance entry. Added by the owner, or automatically when a booking completes."""

    __tablename__ = "service_records"

    vehicle_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("vehicles.id", ondelete="CASCADE"), index=True
    )
    done_on: Mapped[date] = mapped_column(Date)
    description: Mapped[str] = mapped_column(String(300))
    mileage_km: Mapped[int | None] = mapped_column(Integer)
    cost_minor: Mapped[int | None] = mapped_column(BigInteger)
    mechanic_name: Mapped[str | None] = mapped_column(String(160))
    # Set when the record was created by a completed booking (booking module).
    booking_ref: Mapped[str | None] = mapped_column(String(32))

    vehicle: Mapped[Vehicle] = relationship(back_populates="service_records")


# --- Listings -------------------------------------------------------------------------
class ListingType(StrEnum):
    VEHICLE = "vehicle"
    PART = "part"
    MECHANIC_SERVICE = "mechanic_service"
    INSURANCE_PRODUCT = "insurance_product"


class ListingStatus(StrEnum):
    PENDING = "pending"  # waiting for admin review (FR-3.4)
    LIVE = "live"
    RESERVED = "reserved"
    SOLD = "sold"  # also used for "out of stock"
    REJECTED = "rejected"
    SUSPENDED = "suspended"  # taken down by an admin after going live
    REMOVED = "removed"  # deleted by the seller


# Statuses in which a listing is visible to everyone.
PUBLIC_STATUSES = (ListingStatus.LIVE, ListingStatus.RESERVED, ListingStatus.SOLD)


class Listing(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "listings"

    ref_no: Mapped[int] = ref_column(start=5000)
    seller_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    type: Mapped[ListingType] = mapped_column(enum_type(ListingType), index=True)
    status: Mapped[ListingStatus] = mapped_column(
        enum_type(ListingStatus), default=ListingStatus.PENDING, index=True
    )
    title: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    price_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3), default="NGN")
    city: Mapped[str | None] = mapped_column(String(80), index=True)
    # The plain coordinates are what the API returns. `location` is the PostGIS point built
    # from them (always set together, in `service._set_location`) and is what search uses
    # to filter and sort by distance.
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    location: Mapped[Any | None] = mapped_column(Geography("POINT", srid=4326))
    # Type-specific details, e.g. {"make": "Toyota", "model": "Camry", "year": 2018}.
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # Units in stock (parts). None means "not tracked".
    stock: Mapped[int | None] = mapped_column(Integer)
    vin: Mapped[str | None] = mapped_column(String(17), index=True)
    # Warnings shown to the reviewing admin: duplicate_vin, contact_details, duplicate_photos.
    fraud_signals: Mapped[list[str]] = mapped_column(JSONB, default=list)
    rejection_reason: Mapped[str | None] = mapped_column(String(300))
    reviewed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    images: Mapped[list[ListingImage]] = relationship(
        back_populates="listing", cascade="all, delete-orphan", order_by="ListingImage.position"
    )

    @property
    def ref(self) -> str:
        return f"LST-{self.ref_no}"


class ListingImage(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "listing_images"

    listing_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("listings.id", ondelete="CASCADE"), index=True
    )
    storage_key: Mapped[str] = mapped_column(String(300))
    # Fingerprint of the file; the same photo on two sellers' listings is a fraud signal.
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)

    listing: Mapped[Listing] = relationship(back_populates="images")


# --- Inquiries and offers -------------------------------------------------------------
class InquiryStatus(StrEnum):
    OPEN = "open"
    ACCEPTED = "accepted"
    DECLINED = "declined"


class ListingInquiry(UUIDPrimaryKey, Timestamps, Base):
    """A buyer's question or price offer on a listing (FR-3.5)."""

    __tablename__ = "listing_inquiries"

    listing_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("listings.id", ondelete="CASCADE"), index=True
    )
    buyer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    message: Mapped[str] = mapped_column(String(1000))
    offer_minor: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[InquiryStatus] = mapped_column(
        enum_type(InquiryStatus), default=InquiryStatus.OPEN
    )
