"""Request and response bodies for the haulage module."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.modules.payments.models import EscrowStatus, TxStatus


class PlaceIn(BaseModel):
    latitude: float = Field(ge=-90, le=90, examples=[6.5244])
    longitude: float = Field(ge=-180, le=180, examples=[3.3792])
    address: str = Field("", max_length=300, examples=["Third Mainland Bridge, Lagos"])


class EstimateOut(BaseModel):
    distance_km: float
    fare_minor: int
    sos: bool


class JobIn(BaseModel):
    pickup: PlaceIn
    drop: PlaceIn
    vehicle_id: uuid.UUID | None = Field(None, description="A vehicle in your garage.")
    vehicle_description: str = Field("", max_length=200, examples=["Black Toyota Corolla 2014"])
    notes: str | None = Field(None, max_length=500)
    sos: bool = Field(False, description="Emergency: priority dispatch at a higher fare.")


class JobOut(BaseModel):
    id: uuid.UUID
    transaction_id: uuid.UUID
    ref: str = Field(examples=["JOB-80501"])
    status: TxStatus
    escrow_status: EscrowStatus
    is_sos: bool
    payment_due: bool
    customer_id: uuid.UUID
    provider_id: uuid.UUID | None
    vehicle_description: str
    pickup_address: str
    drop_address: str
    distance_km: float
    fare_minor: int
    created_at: datetime


class PresenceIn(BaseModel):
    available: bool
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class PresenceOut(BaseModel):
    available: bool
    latitude: float
    longitude: float
    updated_at: datetime


class OfferOut(BaseModel):
    offer_id: uuid.UUID
    job_id: uuid.UUID
    is_sos: bool
    distance_to_pickup_m: int
    pickup_address: str
    drop_address: str
    fare_minor: int
    expires_at: datetime


class CancelIn(BaseModel):
    reason: str | None = Field(None, max_length=200)


class ProviderLocationOut(BaseModel):
    latitude: float
    longitude: float
    at: datetime


class TrackingOut(BaseModel):
    job_id: uuid.UUID
    status: TxStatus
    provider_location: ProviderLocationOut | None
    remaining_km: float | None
    eta_minutes: int | None


class SosJobOut(BaseModel):
    job_id: uuid.UUID
    ref: str
    status: TxStatus
    customer: str
    provider: str | None
    pickup_address: str
    waiting_minutes: int
    fare_minor: int
