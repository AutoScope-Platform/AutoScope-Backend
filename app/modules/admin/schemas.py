"""Response bodies for the admin module (the dashboard's overview, analytics and audit log)."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.core.audit import AuditCategory


class UserCounts(BaseModel):
    total: int
    new: int = Field(description="Created during the period.")
    suspended: int


class Queues(BaseModel):
    kyc_pending: int
    listings_pending: int
    disputes_open: int
    reports_open: int


class SeriesPoint(BaseModel):
    date: date
    gmv_minor: int


class RecentAction(BaseModel):
    at: datetime
    actor: str
    action: str
    target: str | None


class OverviewOut(BaseModel):
    period_days: int
    users: UserCounts
    listings_live: int
    gmv_minor: int = Field(description="Paid, not refunded, during the period.")
    gmv_all_time_minor: int
    orders_paid: int
    commission_minor: int = Field(description="Commission on escrow released during the period.")
    escrow_held_minor: int
    gmv_by_category: dict[str, int]
    gmv_series: list[SeriesPoint]
    open_sos: int
    queues: Queues
    recent_actions: list[RecentAction]


class SalesPoint(BaseModel):
    date: date
    sales_minor: int


class AnalyticsOut(BaseModel):
    period_days: int
    sales_minor: int
    earned_minor: int = Field(description="Paid out to your wallet (after commission).")
    held_in_escrow_minor: int
    paid_transactions_by_kind: dict[str, int]
    sales_series: list[SalesPoint]
    rating: float
    review_count: int
    live_listings: int


class AuditEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime
    category: AuditCategory
    actor_name: str
    action: str
    target: str | None
    ip_address: str | None


class AuditPage(BaseModel):
    items: list[AuditEntryOut]
    total: int
    page: int
    page_size: int
