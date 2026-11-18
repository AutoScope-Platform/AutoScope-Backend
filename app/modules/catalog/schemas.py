"""Request and response bodies for the catalog module (these also drive the Swagger docs)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.modules.catalog.models import InquiryKind, InquiryStatus, ListingStatus, ListingType


class _Orm(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- Garage ------------------------------------------------------------------------------
class VehicleIn(BaseModel):
    vin: str | None = Field(None, examples=["1HGCM82633A004352"])
    make: str = Field(min_length=1, max_length=60, examples=["Toyota"])
    model: str = Field(min_length=1, max_length=60, examples=["Camry"])
    year: int = Field(ge=1950, le=2100, examples=[2018])
    plate_number: str | None = Field(None, max_length=20)
    colour: str | None = Field(None, max_length=30)
    mileage_km: int | None = Field(None, ge=0)
    fuel_type: str | None = Field(None, max_length=30)


class VehicleUpdateIn(BaseModel):
    vin: str | None = None
    plate_number: str | None = Field(None, max_length=20)
    colour: str | None = Field(None, max_length=30)
    mileage_km: int | None = Field(None, ge=0)
    fuel_type: str | None = Field(None, max_length=30)


class VehicleOut(_Orm):
    id: uuid.UUID
    vin: str | None
    make: str
    model: str
    year: int
    plate_number: str | None
    colour: str | None
    mileage_km: int | None
    fuel_type: str | None
    created_at: datetime


class VinOut(BaseModel):
    vin: str
    make: str | None
    model: str | None
    year: int | None
    body_class: str | None
    fuel_type: str | None
    engine: str | None


class ServiceRecordIn(BaseModel):
    done_on: date
    description: str = Field(min_length=3, max_length=300, examples=["Oil and filter change"])
    mileage_km: int | None = Field(None, ge=0)
    cost_minor: int | None = Field(None, ge=0)
    mechanic_name: str | None = Field(None, max_length=160)


class ServiceRecordOut(_Orm):
    id: uuid.UUID
    done_on: date
    description: str
    mileage_km: int | None
    cost_minor: int | None
    mechanic_name: str | None
    booking_ref: str | None


# --- Listings ----------------------------------------------------------------------------
class FitmentIn(BaseModel):
    """A vehicle range the part fits. Leave `model` out for every model of the make, and a
    year bound out for "any year"."""

    make: str = Field(min_length=1, max_length=60, examples=["Toyota"])
    model: str | None = Field(None, max_length=60, examples=["Camry"])
    year_from: int | None = Field(None, ge=1950, le=2100, examples=[2012])
    year_to: int | None = Field(None, ge=1950, le=2100, examples=[2017])

    @model_validator(mode="after")
    def _years_in_order(self) -> FitmentIn:
        if self.year_from and self.year_to and self.year_from > self.year_to:
            raise ValueError("year_from must not be after year_to")
        return self


class FitmentOut(FitmentIn):
    pass


class ListingIn(BaseModel):
    type: ListingType
    title: str = Field(min_length=3, max_length=160, examples=["2018 Toyota Camry SE, low mileage"])
    description: str = Field("", max_length=5000)
    price_minor: int = Field(ge=0, description="Price in kobo.", examples=[950_000_000])
    city: str | None = Field(None, max_length=80, examples=["Lagos"])
    latitude: float | None = Field(None, ge=-90, le=90)
    longitude: float | None = Field(None, ge=-180, le=180)
    stock: int | None = Field(None, ge=0, description="Units in stock (parts).")
    sku: str | None = Field(None, max_length=64, description="Parts: your own stock code.")
    fits: list[FitmentIn] | None = Field(
        None, max_length=50, description="Parts: the vehicles this part fits (FR-4.1)."
    )
    vin: str | None = Field(None, description="Vehicles only.")
    attributes: dict[str, Any] = Field(
        description="Type-specific details. Vehicles: make, model, year, mileage_km. "
        "Parts: brand, condition. Services: category. Insurance: cover_type.",
        examples=[{"make": "Toyota", "model": "Camry", "year": 2018, "mileage_km": 64000}],
    )


class ListingUpdateIn(BaseModel):
    title: str | None = Field(None, min_length=3, max_length=160)
    description: str | None = Field(None, max_length=5000)
    price_minor: int | None = Field(None, ge=0)
    city: str | None = Field(None, max_length=80)
    latitude: float | None = Field(None, ge=-90, le=90)
    longitude: float | None = Field(None, ge=-180, le=180)
    stock: int | None = Field(None, ge=0)
    sku: str | None = Field(None, max_length=64)
    fits: list[FitmentIn] | None = Field(None, max_length=50, description="Replaces the list.")
    vin: str | None = None
    attributes: dict[str, Any] | None = None


class SellerOut(BaseModel):
    id: uuid.UUID
    name: str
    city: str | None
    verified: bool = Field(description="Identity verification (KYC) approved.")
    rating: float = 0.0
    review_count: int = 0


class ImageOut(BaseModel):
    id: uuid.UUID
    url: str = Field(description="Time-limited link to the photo.")
    position: int


class ListingOut(BaseModel):
    id: uuid.UUID
    ref: str = Field(examples=["LST-5012"])
    type: ListingType
    status: ListingStatus
    title: str
    description: str
    price_minor: int
    currency: str
    city: str | None
    latitude: float | None
    longitude: float | None
    stock: int | None
    sku: str | None = None
    fits: list[FitmentOut] = Field(default_factory=list)
    vin: str | None
    attributes: dict[str, Any]
    images: list[ImageOut]
    seller: SellerOut
    created_at: datetime
    disclosure: str | None = Field(
        None, description="Insurance products only: who underwrites the cover (FSD 5.7)."
    )
    video_url: str | None = Field(None, description="Time-limited link to the listing's video.")
    stats: ListingStatsOut | None = Field(None, description="Seller and staff only.")
    rejection_reason: str | None = Field(None, description="Shown to the seller and staff only.")
    fraud_signals: list[str] = Field(default_factory=list, description="Seller and staff only.")


class ListingPage(BaseModel):
    items: list[ListingOut]
    total: int
    page: int
    page_size: int
    counts_by_type: dict[str, int] = Field(default_factory=dict)


class StatusIn(BaseModel):
    status: ListingStatus


class MechanicOut(BaseModel):
    id: uuid.UUID
    name: str
    city: str | None
    rating: float
    review_count: int
    from_price_minor: int = Field(description="Cheapest matching service, in kobo.")
    services: int = Field(description="Number of matching live services.")
    distance_km: float | None = Field(None, description="Nearest service, when searching near.")


class MechanicPage(BaseModel):
    items: list[MechanicOut]
    total: int


# --- Inquiries ---------------------------------------------------------------------------
class InquiryIn(BaseModel):
    message: str = Field(min_length=2, max_length=1000)
    offer_minor: int | None = Field(None, ge=0, description="Optional price offer, in kobo.")
    kind: InquiryKind | None = Field(
        None,
        description="question, offer, inspection or test_drive (vehicles). Left out: offer "
        "when offer_minor is given, otherwise question.",
    )
    viewing_at: datetime | None = Field(
        None, description="Inspections and test drives: the time you would like, with an offset."
    )


class InquiryOut(_Orm):
    id: uuid.UUID
    listing_id: uuid.UUID
    buyer_id: uuid.UUID
    kind: InquiryKind
    message: str
    offer_minor: int | None
    viewing_at: datetime | None
    status: InquiryStatus
    created_at: datetime


class InquiryAnswerIn(BaseModel):
    accept: bool
    viewing_at: datetime | None = Field(
        None, description="Accepting a viewing: a different time to suggest instead."
    )


class ListingStatsOut(BaseModel):
    """Seller and staff only (FR-3.5)."""

    views: int
    enquiries: int = Field(description="Questions, offers and viewing requests together.")
    offers: int
    viewing_requests: int


class RecommendationOut(BaseModel):
    reason: str = Field(examples=["Fits your 2014 Toyota Camry"])
    listing: ListingOut


class RecommendationsOut(BaseModel):
    items: list[RecommendationOut]


# --- Admin -------------------------------------------------------------------------------
class DecideIn(BaseModel):
    approve: bool
    reason: str | None = Field(None, max_length=300, description="Required when rejecting.")


class SuspendIn(BaseModel):
    reason: str = Field(min_length=3, max_length=300)


class ReviewQueueOut(BaseModel):
    items: list[ListingOut]
    total: int
    counts: dict[str, int]
