"""Request and response bodies for the insurance module."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field

from app.modules.insurance.models import ClaimStatus, PolicyStatus
from app.modules.payments.models import EscrowStatus, TxStatus


class ApplyIn(BaseModel):
    product_id: uuid.UUID = Field(description="A live insurance product listing.")
    vehicle_id: uuid.UUID = Field(description="A vehicle in your garage.")
    details: str = Field("", max_length=2000, examples=["Used daily for ride-hailing"])


class OfferIn(BaseModel):
    premium_minor: int = Field(gt=0, description="Premium in kobo.", examples=[18_500_000])
    terms: str = Field(min_length=3, max_length=4000)
    valid_days: int = Field(14, ge=1, le=90, description="How long the offer stays open.")


class ReasonIn(BaseModel):
    reason: str | None = Field(None, max_length=300)


class AnswerIn(BaseModel):
    accept: bool


class ApplicationOut(BaseModel):
    id: uuid.UUID
    transaction_id: uuid.UUID
    ref: str = Field(examples=["POL-80650"])
    status: TxStatus
    escrow_status: EscrowStatus
    payment_due: bool
    customer_id: uuid.UUID
    insurer_id: uuid.UUID
    product_title: str
    cover_type: str
    vehicle_summary: str
    details: str
    premium_minor: int | None
    terms: str | None
    offer_valid_until: datetime | None
    decline_reason: str | None
    is_renewal: bool
    created_at: datetime


class ApplicationPage(BaseModel):
    items: list[ApplicationOut]
    total: int


class PolicyOut(BaseModel):
    id: uuid.UUID
    policy_number: str
    status: PolicyStatus
    product_title: str
    cover_type: str
    vehicle_summary: str
    premium_minor: int
    starts_on: date
    ends_on: date
    holder_id: uuid.UUID
    insurer_id: uuid.UUID


class ClaimIn(BaseModel):
    incident_date: date
    description: str = Field(min_length=10, max_length=4000)
    amount_claimed_minor: int = Field(gt=0)


class ClaimDecisionIn(BaseModel):
    status: ClaimStatus
    approved_amount_minor: int | None = Field(None, gt=0, description="Required when approving.")
    note: str | None = Field(None, max_length=500)


class ClaimDocumentOut(BaseModel):
    id: uuid.UUID
    filename: str
    content_type: str
    url: str


class ClaimOut(BaseModel):
    id: uuid.UUID
    ref: str = Field(examples=["CLM-612"])
    policy_id: uuid.UUID
    status: ClaimStatus
    incident_date: date
    description: str
    amount_claimed_minor: int
    approved_amount_minor: int | None
    insurer_note: str | None
    decided_at: datetime | None
    created_at: datetime
    documents: list[ClaimDocumentOut] = []


class ClaimPage(BaseModel):
    items: list[ClaimOut]
    total: int
