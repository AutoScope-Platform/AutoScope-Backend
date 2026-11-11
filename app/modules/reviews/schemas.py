"""Request and response bodies for the reviews module."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.modules.reviews.models import ReportReason, ReportStatus, ReportTarget


class ReviewIn(BaseModel):
    transaction_ref: str = Field(
        examples=["BKG-1042"], description="Reference of the completed sale."
    )
    rating: int = Field(ge=1, le=5)
    comment: str | None = Field(None, max_length=2000)


class ReplyIn(BaseModel):
    reply: str = Field(min_length=1, max_length=1000)


class ReviewOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    author_name: str = ""
    transaction_ref: str
    rating: int
    comment: str | None
    reply: str | None
    replied_at: datetime | None
    created_at: datetime


class ReviewPage(BaseModel):
    items: list[ReviewOut]
    total: int
    average: float
    count: int


class ReportIn(BaseModel):
    target_type: ReportTarget
    target_id: uuid.UUID
    reason: ReportReason
    details: str | None = Field(None, max_length=1000)


class ReportOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    ref: str = Field(examples=["RPT-3012"])
    target_type: ReportTarget
    target_id: uuid.UUID
    reason: ReportReason
    details: str | None
    status: ReportStatus
    created_at: datetime


class ResolveReportIn(BaseModel):
    action: bool = Field(description="true = remove/suspend the item; false = dismiss.")


class ReportPage(BaseModel):
    items: list[ReportOut]
    total: int
    counts: dict[str, int]
