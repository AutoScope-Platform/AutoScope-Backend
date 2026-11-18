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
    flagged_users: int = Field(0, description="Accounts on the repeat-offender list.")


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


class RiskSignalsOut(BaseModel):
    suspended_listings: int
    upheld_reports: int
    disputes_lost: int
    rejected_listings: int
    fraud_flagged_listings: int


class RiskFlagOut(BaseModel):
    user_id: uuid.UUID
    ref: str
    name: str
    status: str
    score: int = Field(description="Weighted strikes; 3 or more is flagged.")
    signals: RiskSignalsOut
    reasons: list[str] = Field(examples=[["2 listing(s) taken down", "1 report(s) upheld"]])


class RiskFlagPage(BaseModel):
    days: int
    items: list[RiskFlagOut]


class RemoveUserIn(BaseModel):
    reason: str = Field(min_length=3, max_length=300, description="Kept in the audit log.")
