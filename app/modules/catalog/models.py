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
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
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
    __table_args__ = (
        # Keyword search is `ILIKE '%word%'` on title and description; trigram indexes let
        # Postgres find matches without reading every row (FSD 5.1, see scripts/loadtest_search.py).
        Index("ix_listings_title_trgm", "title", postgresql_using="gin",
              postgresql_ops={"title": "gin_trgm_ops"}),
        Index("ix_listings_description_trgm", "description", postgresql_using="gin",
              postgresql_ops={"description": "gin_trgm_ops"}),
        # Search also matches the SKU; without this index the OR makes Postgres scan the table.
        Index("ix_listings_sku_trgm", "sku", postgresql_using="gin",
              postgresql_ops={"sku": "gin_trgm_ops"}),
        # The default browse: live listings, newest first.
        Index("ix_listings_status_created_at", "status", "created_at"),
    )  # fmt: skip

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
    # Parts: the seller's own stock-keeping code, unique among their active listings.
    sku: Mapped[str | None] = mapped_column(String(64), index=True)
    # One short video per listing (FR-3.1); the key in object storage and its type.
    video_key: Mapped[str | None] = mapped_column(String(300))
    video_type: Mapped[str | None] = mapped_column(String(40))
    # Times the listing page was opened by someone other than the seller (FR-3.5).
    view_count: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    # Parts: alert the seller when stock falls to this level (FR-4.5); None = no alert.
    low_stock_threshold: Mapped[int | None] = mapped_column(Integer)
    # Set when the alert went out, cleared when stock is back above the threshold, so the
    # seller is told once per time they run low.
    low_stock_alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
    # Parts only: which vehicles the part fits (FR-4.1).
    fitments: Mapped[list[PartFitment]] = relationship(
        back_populates="listing", cascade="all, delete-orphan", order_by="PartFitment.make"
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


class PartFitment(UUIDPrimaryKey, Base):
    """One vehicle range a part fits, e.g. Toyota Camry 2012-2017 (FR-4.1, FR-4.2).

    `model` empty means every model of the make; an empty year bound is open-ended. A
    part can have several rows (the same brake pad often fits a Camry and a Venza).
    """

    __tablename__ = "part_fitments"

    listing_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("listings.id", ondelete="CASCADE"), index=True
    )
    make: Mapped[str] = mapped_column(String(60), index=True)
    model: Mapped[str | None] = mapped_column(String(60))
    year_from: Mapped[int | None] = mapped_column(Integer)
    year_to: Mapped[int | None] = mapped_column(Integer)

    listing: Mapped[Listing] = relationship(back_populates="fitments")


# --- Inquiries and offers -------------------------------------------------------------
class InquiryStatus(StrEnum):
    OPEN = "open"
    ACCEPTED = "accepted"
    DECLINED = "declined"


class InquiryKind(StrEnum):
    QUESTION = "question"
    OFFER = "offer"
    INSPECTION = "inspection"  # FR-3.4: see the car, or have a mechanic look at it
    TEST_DRIVE = "test_drive"


VIEWING_KINDS = (InquiryKind.INSPECTION, InquiryKind.TEST_DRIVE)


class ListingInquiry(UUIDPrimaryKey, Timestamps, Base):
    """A buyer's question or price offer on a listing (FR-3.5)."""

    __tablename__ = "listing_inquiries"

    listing_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("listings.id", ondelete="CASCADE"), index=True
    )
    buyer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    message: Mapped[str] = mapped_column(String(1000))
    offer_minor: Mapped[int | None] = mapped_column(BigInteger)
    kind: Mapped[InquiryKind] = mapped_column(
        enum_type(InquiryKind), default=InquiryKind.QUESTION, server_default="question"
    )
    # Inspections and test drives: the time asked for (or the seller's new time once accepted).
    viewing_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[InquiryStatus] = mapped_column(
        enum_type(InquiryStatus), default=InquiryStatus.OPEN
    )


# --- Part requests (FR-4.6) -------------------------------------------------------------
class PartRequestStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class PartRequest(UUIDPrimaryKey, Timestamps, Base):
    """A buyer asking for a part nobody has listed, or for a compatibility check (FR-4.6).
    Parts sellers who stock that make are told and answer with a quote."""

    __tablename__ = "part_requests"

    ref_no: Mapped[int] = ref_column(start=7000)
    buyer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    vehicle_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("vehicles.id", ondelete="SET NULL")
    )
    make: Mapped[str] = mapped_column(String(60), index=True)
    model: Mapped[str | None] = mapped_column(String(60))
    year: Mapped[int | None] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[PartRequestStatus] = mapped_column(
        enum_type(PartRequestStatus), default=PartRequestStatus.OPEN, index=True
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    quotes: Mapped[list[PartQuote]] = relationship(
        back_populates="request", cascade="all, delete-orphan", order_by="PartQuote.price_minor"
    )

    @property
    def ref(self) -> str:
        return f"PRQ-{self.ref_no}"


class PartQuote(UUIDPrimaryKey, Timestamps, Base):
    """One seller's answer to a part request; a seller has one quote per request (revisable)."""

    __tablename__ = "part_quotes"
    __table_args__ = (UniqueConstraint("request_id", "provider_id"),)

    request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("part_requests.id", ondelete="CASCADE"), index=True
    )
    provider_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    price_minor: Mapped[int] = mapped_column(BigInteger)
    message: Mapped[str] = mapped_column(String(500), default="")
    # Optional: a live listing the buyer can put in the cart straight away.
    listing_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("listings.id", ondelete="SET NULL")
    )

    request: Mapped[PartRequest] = relationship(back_populates="quotes")
