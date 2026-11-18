"""Which events notify whom, and what the message says.

Each rule is a function `(db, payload) -> list[Notice]`. To notify about a new event:
1. make sure the owning service publishes it (`publish("thing.happened", ...)`),
2. add a function here and list it in `RULES`.

Keep the wording short: it becomes a push notification title and body.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.modules.catalog.models import Listing
from app.modules.payments.models import Dispute, Transaction


@dataclass
class Notice:
    user_id: uuid.UUID
    title: str
    body: str
    data: dict[str, str] = field(default_factory=dict)
    # Channels beyond the in-app list. "push" is cheap; "sms" and "email" are for things
    # people must not miss (money, verification).
    channels: tuple[str, ...] = ("push",)
    inbox: bool = True  # False = push only (chat messages have their own inbox)


Rule = Callable[[Session, dict[str, Any]], list[Notice]]


def _uid(value: str | None) -> uuid.UUID | None:
    return uuid.UUID(value) if value else None


def _tx(db: Session, payload: dict) -> Transaction | None:
    return db.get(Transaction, _uid(payload.get("transaction_id")))


# --- Listings and enquiries ------------------------------------------------------------
def listing_approved(db, p):
    return [
        Notice(
            _uid(p["seller_id"]),
            "Your listing is live",
            f"{p['ref']} is now visible to buyers.",
            {"listing_id": p["listing_id"]},
        )
    ]


def listing_rejected(db, p):
    listing = db.get(Listing, _uid(p["listing_id"]))
    why = f" Reason: {listing.rejection_reason}" if listing and listing.rejection_reason else ""
    return [
        Notice(
            _uid(p["seller_id"]),
            "Your listing was not approved",
            f"{p['ref']} needs changes.{why}",
            {"listing_id": p["listing_id"]},
        )
    ]


def listing_suspended(db, p):
    return [
        Notice(
            _uid(p["seller_id"]),
            "A listing was taken down",
            "One of your listings was suspended. Open it to see why.",
            {"listing_id": p["listing_id"]},
            ("push", "email"),
        )
    ]


def inquiry_created(db, p):
    listing = db.get(Listing, _uid(p["listing_id"]))
    return (
        [
            Notice(
                listing.seller_id,
                "New enquiry",
                f"Someone asked about {listing.title}.",
                {"listing_id": p["listing_id"]},
            )
        ]
        if listing
        else []
    )


def inquiry_answered(db, p):
    verb = "accepted" if p["accepted"] else "declined"
    return [
        Notice(
            _uid(p["buyer_id"]),
            f"Offer {verb}",
            f"The seller {verb} your offer or question.",
            {"inquiry_id": p["inquiry_id"]},
        )
    ]


# --- Orders ----------------------------------------------------------------------------
def order_created(db, p):
    return [
        Notice(
            _uid(p["seller_id"]),
            "New order",
            f"{p['ref']} was placed. It ships once the buyer pays.",
            {"order_id": p["order_id"]},
        )
    ]


def order_paid(db, p):
    return [
        Notice(
            _uid(p["seller_id"]),
            "Order paid",
            f"{p['ref']} is paid. Ship it to get paid.",
            {"order_id": p["order_id"]},
            ("push", "sms"),
        )
    ]


def order_shipped(db, p):
    return [
        Notice(
            _uid(p["buyer_id"]),
            "Your order has shipped",
            f"{p['ref']} is on its way. Confirm when it arrives.",
            {"order_id": p["order_id"]},
        )
    ]


def order_completed(db, p):
    return [
        Notice(
            _uid(p["seller_id"]),
            "Delivery confirmed",
            f"The buyer confirmed {p['ref']}. Payment follows the dispute window.",
            {"order_id": p["order_id"]},
        )
    ]


def cancelled(db, p):
    ref = p.get("ref", "")
    return [
        Notice(
            _uid(p["notify_id"]),
            "Cancelled",
            f"{ref} was cancelled.".strip(),
            {k: v for k, v in p.items() if k.endswith("_id")},
            ("push", "email"),
        )
    ]


# --- Money -----------------------------------------------------------------------------
def payment_succeeded(db, p):
    tx = _tx(db, p)
    if tx is None:
        return []
    out = [
        Notice(
            tx.customer_id,
            "Payment received",
            f"We received your payment for {tx.ref}.",
            {"transaction_id": str(tx.id)},
            ("push", "email"),
        )
    ]
    if tx.provider_id:
        out.append(
            Notice(
                tx.provider_id,
                "Payment held in escrow",
                f"{tx.ref} is paid and held safely until the job is done.",
                {"transaction_id": str(tx.id)},
            )
        )
    return out


def escrow_released(db, p):
    tx = _tx(db, p)
    return (
        [
            Notice(
                tx.provider_id,
                "Funds released",
                f"Earnings for {tx.ref} were added to your wallet.",
                {"transaction_id": str(tx.id)},
                ("push", "email"),
            )
        ]
        if tx and tx.provider_id
        else []
    )


def escrow_refunded(db, p):
    tx = _tx(db, p)
    return (
        [
            Notice(
                tx.customer_id,
                "Refund issued",
                f"Your payment for {tx.ref} is being refunded.",
                {"transaction_id": str(tx.id)},
                ("push", "sms", "email"),
            )
        ]
        if tx
        else []
    )


def dispute_opened(db, p):
    d = db.get(Dispute, _uid(p["dispute_id"]))
    if d is None:
        return []
    other = d.provider_id if d.opened_by_id == d.customer_id else d.customer_id
    return [
        Notice(
            other,
            "A dispute was opened",
            f"{d.ref}: {d.reason}. Payment is on hold.",
            {"dispute_id": str(d.id)},
            ("push", "email"),
        )
    ]


def dispute_resolved(db, p):
    d = db.get(Dispute, _uid(p["dispute_id"]))
    return (
        [
            Notice(
                u,
                "Dispute resolved",
                f"{d.ref} was resolved by support.",
                {"dispute_id": str(d.id)},
                ("push", "email"),
            )
            for u in (d.customer_id, d.provider_id)
        ]
        if d
        else []
    )


# --- Bookings --------------------------------------------------------------------------
def _booking(title: str, body: str, to: str, channels=("push",)):
    def rule(db, p):
        return [
            Notice(_uid(p[to]), title, body.format(**p), {"booking_id": p["booking_id"]}, channels)
        ]

    return rule


# --- Haulage ---------------------------------------------------------------------------
def job_offered(db, p):
    sos = p.get("sos")
    return [
        Notice(
            _uid(p["provider_id"]),
            "SOS job nearby" if sos else "New job nearby",
            "Open the app to accept before someone else does.",
            {"job_id": p["job_id"]},
            ("push", "sms") if sos else ("push",),
        )
    ]


def _job(title: str, body: str, to: str = "customer_id", channels=("push",)):
    def rule(db, p):
        return [Notice(_uid(p[to]), title, body.format(**p), {"job_id": p["job_id"]}, channels)]

    return rule


# --- Insurance -------------------------------------------------------------------------
def _simple(title: str, body: str, to: str, key: str, channels=("push",)):
    """A one-recipient rule whose data link is `{key}: payload[key]`."""

    def rule(db, p):
        return [Notice(_uid(p[to]), title, body.format(**p), {key: p[key]}, channels)]

    return rule


def claim_updated(db, p):
    return [
        Notice(
            _uid(p["claimant_id"]),
            "Claim update",
            f"{p['ref']} is now {p['status'].replace('_', ' ')}.",
            {"claim_id": p["claim_id"]},
            ("push", "email"),
        )
    ]


def review_created(db, p):
    return [
        Notice(
            _uid(p["provider_id"]),
            "New review",
            "A customer left you a review.",
            {"review_id": p["review_id"]},
        )
    ]


def message_sent(db, p):
    return [
        Notice(
            _uid(p["recipient_id"]),
            p["sender_name"],
            p["preview"] or "New message",
            {"thread_id": p["thread_id"]},
            inbox=False,
        )
    ]


# --- Accounts --------------------------------------------------------------------------
def kyc_decided(db, p):
    if p["approved"]:
        return [
            Notice(
                _uid(p["user_id"]),
                "You are verified",
                "Your identity check passed. You can now list and sell.",
                {},
                ("push", "email", "sms"),
            )
        ]
    return [
        Notice(
            _uid(p["user_id"]),
            "Verification not approved",
            f"{p.get('reason') or 'Please resubmit your documents.'}",
            {},
            ("push", "email", "sms"),
        )
    ]


def user_status_changed(db, p):
    suspended = p["status"] == "suspended"
    title = "Account suspended" if suspended else "Account reactivated"
    body = (
        "Contact support if you think this is a mistake." if suspended else "You can sign in again."
    )
    return [Notice(_uid(p["user_id"]), title, body, {}, ("email",))]


# event name -> rule
RULES: dict[str, Rule] = {
    "listing.approved": listing_approved,
    "listing.rejected": listing_rejected,
    "listing.suspended": listing_suspended,
    "inquiry.created": inquiry_created,
    "inquiry.answered": inquiry_answered,
    "order.created": order_created,
    "order.paid": order_paid,
    "order.shipped": order_shipped,
    "order.completed": order_completed,
    "order.cancelled": cancelled,
    "booking.cancelled": cancelled,
    "job.cancelled": cancelled,
    "payment.succeeded": payment_succeeded,
    "escrow.released": escrow_released,
    "escrow.refunded": escrow_refunded,
    "dispute.opened": dispute_opened,
    "dispute.resolved": dispute_resolved,
    "booking.requested": _booking(
        "New booking request", "{ref}: a customer wants to book you.", "mechanic_id"
    ),
    "booking.accepted": _booking(
        "Booking accepted",
        "{ref} was accepted. Pay to confirm your slot.",
        "customer_id",
        ("push", "sms"),
    ),
    "booking.declined": _booking(
        "Booking declined", "{ref} was declined by the mechanic.", "customer_id"
    ),
    "booking.quoted": _booking(
        "You have a quote",
        "{ref}: the mechanic sent a quote to review.",
        "customer_id",
        ("push", "email"),
    ),
    "booking.quote_answered": _booking(
        "Quote answered", "{ref}: the customer answered your quote.", "mechanic_id"
    ),
    "booking.time_proposed": _booking(
        "New time suggested",
        "{ref}: the mechanic suggested a different time. Please accept or decline it.",
        "customer_id",
        ("push", "sms"),
    ),
    "booking.time_answered": _booking(
        "Time answered", "{ref}: the customer answered your suggested time.", "mechanic_id"
    ),
    "booking.started": _booking("Work started", "{ref}: the mechanic started work.", "customer_id"),
    "booking.completed": _booking(
        "Work completed", "{ref} is done. Please leave a review.", "customer_id", ("push", "email")
    ),
    "job.offered": job_offered,
    "job.assigned": _job("Driver found", "A driver accepted your job and is on the way."),
    "job.en_route": _job("Driver on the way", "Your driver is heading to the pickup point."),
    "job.in_transit": _job("Vehicle loaded", "Your vehicle is on its way to the drop-off."),
    "job.completed": _job(
        "Delivered", "Your job is complete. Please leave a review.", channels=("push", "email")
    ),
    "insurance.applied": _simple(
        "New insurance application",
        "{ref}: a customer applied for cover.",
        "insurer_id",
        "application_id",
    ),
    "insurance.offered": _simple(
        "You have an insurance offer",
        "{ref}: review the premium and terms.",
        "customer_id",
        "application_id",
        ("push", "email"),
    ),
    "insurance.declined": _simple(
        "Application declined",
        "{ref} was declined by the insurer.",
        "customer_id",
        "application_id",
    ),
    "insurance.answered": _simple(
        "Offer answered", "{ref}: the customer answered your offer.", "insurer_id", "application_id"
    ),
    "policy.issued": _simple(
        "Policy issued",
        "{ref}: your policy is active. Open it for the certificate.",
        "holder_id",
        "policy_id",
        ("push", "email"),
    ),
    "policy.expiring": _simple(
        "Policy expiring soon",
        "Your policy ends on {ends_on}. Renew to stay covered.",
        "holder_id",
        "policy_id",
        ("push", "sms", "email"),
    ),
    "claim.submitted": _simple(
        "New claim", "{ref}: a policy holder submitted a claim.", "insurer_id", "claim_id"
    ),
    "claim.updated": claim_updated,
    "review.created": review_created,
    "message.sent": message_sent,
    "kyc.decided": kyc_decided,
    "user.status_changed": user_status_changed,
}
