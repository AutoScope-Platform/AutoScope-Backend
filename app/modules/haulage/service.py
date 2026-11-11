"""Haulage business logic: fare estimate, requesting, dispatch, the trip, tracking, SOS board.

Lifecycle (status lives on the transaction):

    pending --paid--> searching --a provider accepts--> confirmed --> en_route --> in_transit --> completed
    (the customer can cancel up to pickup; a paid job is refunded in full)

Dispatch: when a job is paid, the nearest available providers (PostGIS distance to the pickup
point) each get an offer. The first to accept gets the job. If nobody accepts, the scheduled
job `redispatch_searching` searches again over a wider area. SOS jobs search wider, offer to
more providers and cost more.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from geoalchemy2 import WKTElement
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import db as db_module
from app.core.db import utcnow
from app.core.errors import Conflict, NotFound, UnprocessableEntity
from app.core.events import Event, publish, subscribe
from app.core.permissions import ADMIN_ROLES, Role
from app.core.realtime import Hub
from app.modules.catalog import service as catalog
from app.modules.haulage.models import (
    HaulageJob,
    JobOffer,
    OfferStatus,
    ProviderLocation,
)
from app.modules.identity.models import KycStatus, User, UserRole
from app.modules.payments import service as payments
from app.modules.payments.models import FINAL_STATUSES, Transaction, TransactionKind, TxStatus

# Live tracking connections, keyed by job id (see app/core/realtime.py).
hub = Hub()

# --- Pricing and dispatch settings (kobo / km / minutes). Change here to retune. -------------
BASE_FARE_MINOR = 3_000_00  # N3,000 call-out
PER_KM_MINOR = 250_00  # N250 per km
MIN_FARE_MINOR = 5_000_00
SOS_MULTIPLIER = 1.5
ROAD_FACTOR = 1.3  # straight-line distance x 1.3 approximates road distance
AVERAGE_SPEED_KMH = 30  # city traffic, used for ETAs
RADIUS_KM, SOS_RADIUS_KM = 10, 25  # first search radius; it grows with each round
OFFERS_PER_ROUND, SOS_OFFERS_PER_ROUND = 5, 10
OFFER_TTL = timedelta(minutes=2)
LOCATION_FRESH_FOR = timedelta(minutes=30)  # ignore providers whose app went quiet
MAX_ROUNDS = 4
ACTIVE_STATUSES = (TxStatus.CONFIRMED, TxStatus.EN_ROUTE, TxStatus.IN_TRANSIT)


def _point(lat: float, lng: float):
    """A PostGIS geography value for a coordinate."""
    return func.ST_GeogFromText(f"SRID=4326;POINT({lng} {lat})")


# ======================================================================================
# Estimate
# ======================================================================================
def estimate_fare(
    db: Session, pickup: tuple[float, float], drop: tuple[float, float], sos: bool
) -> tuple[float, int]:
    """Returns (road distance in km, fare in kobo)."""
    metres = db.scalar(select(func.ST_Distance(_point(*pickup), _point(*drop)))) or 0.0
    km = round(metres / 1000 * ROAD_FACTOR, 1)
    fare = max(BASE_FARE_MINOR + int(km * PER_KM_MINOR), MIN_FARE_MINOR)
    if sos:
        fare = int(fare * SOS_MULTIPLIER)
    return km, round(fare, -2)  # round to the nearest N1


# ======================================================================================
# Provider presence
# ======================================================================================
def set_presence(
    db: Session, provider: User, *, available: bool, latitude: float, longitude: float
) -> ProviderLocation:
    """Go online/offline and report a position. Called repeatedly by the provider's app."""
    row = db.get(ProviderLocation, provider.id) or ProviderLocation(user_id=provider.id)
    row.available, row.latitude, row.longitude = available, latitude, longitude
    row.location = WKTElement(f"POINT({longitude} {latitude})", srid=4326)
    row.updated_at = utcnow()
    db.add(row)
    # If they are on a trip, the customer sees the movement.
    job = _active_job_of(db, provider.id)
    if job:
        job.last_lat, job.last_lng, job.last_location_at = latitude, longitude, row.updated_at
    db.commit()
    if job:
        hub.broadcast(job.id, tracking_payload(db, job))
    return row


def _active_job_of(db: Session, provider_id: uuid.UUID) -> HaulageJob | None:
    return db.scalar(
        select(HaulageJob)
        .join(Transaction, Transaction.id == HaulageJob.transaction_id)
        .where(HaulageJob.provider_id == provider_id, Transaction.status.in_(ACTIVE_STATUSES))
    )


# ======================================================================================
# Requesting
# ======================================================================================
def request_job(
    db: Session,
    customer: User,
    *,
    pickup: tuple[float, float, str],
    drop: tuple[float, float, str],
    vehicle_id: uuid.UUID | None,
    vehicle_description: str,
    notes: str | None,
    sos: bool,
) -> HaulageJob:
    """Create a job and its transaction. The customer then pays the fare (`POST /payments`);
    dispatch starts when the payment succeeds."""
    if vehicle_id:
        vehicle = catalog.get_vehicle(db, customer, vehicle_id)
        vehicle_description = (
            vehicle_description or f"{vehicle.year} {vehicle.make} {vehicle.model}"
        )
    km, fare = estimate_fare(db, pickup[:2], drop[:2], sos)
    tx = payments.create_transaction(
        db,
        kind=TransactionKind.HAULAGE_JOB,
        customer_id=customer.id,
        provider_id=None,
        amount_minor=fare,
        is_sos=sos,
        payment_due=True,
    )
    job = HaulageJob(
        transaction_id=tx.id,
        customer_id=customer.id,
        is_sos=sos,
        vehicle_id=vehicle_id,
        vehicle_description=vehicle_description,
        notes=notes,
        pickup_lat=pickup[0],
        pickup_lng=pickup[1],
        pickup_address=pickup[2],
        pickup_location=WKTElement(f"POINT({pickup[1]} {pickup[0]})", srid=4326),
        drop_lat=drop[0],
        drop_lng=drop[1],
        drop_address=drop[2],
        distance_km=km,
        fare_minor=fare,
    )
    db.add(job)
    db.commit()
    return job


# ======================================================================================
# Dispatch
# ======================================================================================
@subscribe("payment.succeeded")
def _on_payment_succeeded(event: Event) -> None:
    """A paid job starts searching for a provider."""
    if event.payload.get("kind") != TransactionKind.HAULAGE_JOB.value:
        return
    db = db_module.new_session()  # looked up at call time so tests can swap it
    try:
        tx = db.get(Transaction, uuid.UUID(event.payload["transaction_id"]))
        job = (
            db.scalar(select(HaulageJob).where(HaulageJob.transaction_id == tx.id)) if tx else None
        )
        if job and tx.status == TxStatus.PENDING:
            tx.status = TxStatus.SEARCHING
            dispatch(db, job)
            db.commit()
            publish(
                "job.searching",
                job_id=str(job.id),
                customer_id=str(job.customer_id),
                sos=job.is_sos,
            )
    finally:
        db.close()


def dispatch(db: Session, job: HaulageJob) -> int:
    """Offer the job to the nearest available providers. Returns how many were offered."""
    job.dispatch_rounds += 1
    radius_km = (SOS_RADIUS_KM if job.is_sos else RADIUS_KM) * job.dispatch_rounds
    limit = SOS_OFFERS_PER_ROUND if job.is_sos else OFFERS_PER_ROUND
    point = _point(job.pickup_lat, job.pickup_lng)
    distance = func.ST_Distance(ProviderLocation.location, point)

    busy = (
        select(HaulageJob.provider_id)
        .join(Transaction, Transaction.id == HaulageJob.transaction_id)
        .where(Transaction.status.in_(ACTIVE_STATUSES), HaulageJob.provider_id.is_not(None))
    )
    already_offered = select(JobOffer.provider_id).where(JobOffer.job_id == job.id)
    rows = db.execute(
        select(ProviderLocation.user_id, distance)
        .join(User, User.id == ProviderLocation.user_id)
        .join(UserRole, (UserRole.user_id == User.id) & (UserRole.role == Role.HAULAGE_PROVIDER))
        .where(
            UserRole.active.is_(True),
            User.kyc_status == KycStatus.APPROVED,
            ProviderLocation.available.is_(True),
            ProviderLocation.updated_at >= utcnow() - LOCATION_FRESH_FOR,
            func.ST_DWithin(ProviderLocation.location, point, radius_km * 1000),
            ProviderLocation.user_id.not_in(busy),
            ProviderLocation.user_id.not_in(already_offered),
        )
        .order_by(distance)
        .limit(limit)
    ).all()
    for provider_id, metres in rows:
        db.add(
            JobOffer(
                job_id=job.id,
                provider_id=provider_id,
                distance_m=int(metres),
                expires_at=utcnow() + OFFER_TTL,
            )
        )
    db.flush()
    for provider_id, _ in rows:
        publish("job.offered", job_id=str(job.id), provider_id=str(provider_id), sos=job.is_sos)
    return len(rows)


def redispatch_searching(db: Session) -> int:
    """Scheduled job (`app/worker.py`, every minute): expire stale offers and search again,
    over a wider area, for jobs still without a provider. Returns the number of new offers."""
    now = utcnow()
    for offer in db.scalars(
        select(JobOffer).where(JobOffer.status == OfferStatus.PENDING, JobOffer.expires_at <= now)
    ):
        offer.status = OfferStatus.EXPIRED
    db.flush()
    pending_jobs = select(JobOffer.job_id).where(JobOffer.status == OfferStatus.PENDING)
    offered = 0
    for job in db.scalars(
        select(HaulageJob)
        .join(Transaction, Transaction.id == HaulageJob.transaction_id)
        .where(
            Transaction.status == TxStatus.SEARCHING,
            HaulageJob.dispatch_rounds < MAX_ROUNDS,
            HaulageJob.id.not_in(pending_jobs),
        )
    ):
        offered += dispatch(db, job)
    db.commit()
    return offered


# ======================================================================================
# Provider: offers and the trip
# ======================================================================================
def list_offers(db: Session, provider: User) -> list[tuple[JobOffer, HaulageJob]]:
    rows = db.execute(
        select(JobOffer, HaulageJob)
        .join(HaulageJob, HaulageJob.id == JobOffer.job_id)
        .where(
            JobOffer.provider_id == provider.id,
            JobOffer.status == OfferStatus.PENDING,
            JobOffer.expires_at > utcnow(),
        )
        .order_by(HaulageJob.is_sos.desc(), JobOffer.distance_m)
    ).all()
    return [(o, j) for o, j in rows]


def _my_offer(db: Session, provider: User, offer_id: uuid.UUID) -> JobOffer:
    offer = db.get(JobOffer, offer_id)
    if offer is None or offer.provider_id != provider.id:
        raise NotFound("Offer not found", code="offer_not_found")
    return offer


def accept_offer(db: Session, provider: User, offer_id: uuid.UUID) -> HaulageJob:
    """First provider to accept gets the job; the job row lock makes that race safe."""
    offer = _my_offer(db, provider, offer_id)
    job = db.scalar(select(HaulageJob).where(HaulageJob.id == offer.job_id).with_for_update())
    tx = payments.get_transaction(db, job.transaction_id, lock=True)
    if offer.status != OfferStatus.PENDING or offer.expires_at <= utcnow():
        raise UnprocessableEntity("This offer has expired", code="offer_expired")
    if job.provider_id is not None or tx.status != TxStatus.SEARCHING:
        raise Conflict("Another provider already took this job", code="job_taken")
    if _active_job_of(db, provider.id):
        raise UnprocessableEntity("Finish your current job first", code="busy")

    job.provider_id, job.accepted_at = provider.id, utcnow()
    tx.provider_id, tx.status = provider.id, TxStatus.CONFIRMED
    for other in db.scalars(select(JobOffer).where(JobOffer.job_id == job.id)):
        other.status = OfferStatus.ACCEPTED if other.id == offer.id else OfferStatus.EXPIRED
    location = db.get(ProviderLocation, provider.id)
    if location:
        job.last_lat, job.last_lng, job.last_location_at = (
            location.latitude,
            location.longitude,
            utcnow(),
        )
    db.commit()
    publish(
        "job.assigned",
        job_id=str(job.id),
        customer_id=str(job.customer_id),
        provider_id=str(provider.id),
    )
    hub.broadcast(job.id, tracking_payload(db, job))
    return job


def decline_offer(db: Session, provider: User, offer_id: uuid.UUID) -> None:
    offer = _my_offer(db, provider, offer_id)
    if offer.status == OfferStatus.PENDING:
        offer.status = OfferStatus.DECLINED
        db.commit()


def _provider_job(db: Session, provider: User, job_id: uuid.UUID) -> tuple[HaulageJob, Transaction]:
    job = db.get(HaulageJob, job_id)
    if job is None or job.provider_id != provider.id:
        raise NotFound("Job not found", code="job_not_found")
    return job, payments.get_transaction(db, job.transaction_id, lock=True)


def _need(tx: Transaction, *statuses: TxStatus) -> None:
    if tx.status not in statuses:
        raise UnprocessableEntity(
            f"This job is {tx.status.value.replace('_', ' ')}, so that is not possible now",
            code="invalid_status",
        )


def start_trip(db: Session, provider: User, job_id: uuid.UUID) -> HaulageJob:
    """Provider is on the way to the pickup point."""
    job, tx = _provider_job(db, provider, job_id)
    _need(tx, TxStatus.CONFIRMED)
    tx.status = TxStatus.EN_ROUTE
    db.commit()
    publish("job.en_route", job_id=str(job.id), customer_id=str(job.customer_id))
    hub.broadcast(job.id, tracking_payload(db, job))
    return job


def pick_up(db: Session, provider: User, job_id: uuid.UUID) -> HaulageJob:
    """Vehicle loaded, heading to the drop-off."""
    job, tx = _provider_job(db, provider, job_id)
    _need(tx, TxStatus.EN_ROUTE)
    tx.status, job.picked_up_at = TxStatus.IN_TRANSIT, utcnow()
    db.commit()
    publish("job.in_transit", job_id=str(job.id), customer_id=str(job.customer_id))
    hub.broadcast(job.id, tracking_payload(db, job))
    return job


def complete_job(db: Session, provider: User, job_id: uuid.UUID) -> HaulageJob:
    """Delivered. Starts the dispute window; the escrow pays the provider after it."""
    job, tx = _provider_job(db, provider, job_id)
    _need(tx, TxStatus.IN_TRANSIT)
    payments.mark_completed(db, tx, TxStatus.COMPLETED)
    db.commit()
    publish("job.completed", job_id=str(job.id), customer_id=str(job.customer_id), ref=tx.ref)
    hub.broadcast(job.id, tracking_payload(db, job))
    return job


# ======================================================================================
# Customer
# ======================================================================================
def cancel_job(db: Session, customer: User, job_id: uuid.UUID, reason: str | None) -> HaulageJob:
    """Cancel up to pickup. Paid jobs are refunded in full."""
    job = db.get(HaulageJob, job_id)
    if job is None or job.customer_id != customer.id:
        raise NotFound("Job not found", code="job_not_found")
    tx = payments.get_transaction(db, job.transaction_id, lock=True)
    _need(tx, TxStatus.PENDING, TxStatus.SEARCHING, TxStatus.CONFIRMED, TxStatus.EN_ROUTE)
    payments.cancel_and_refund(db, tx, reason=reason or "Job cancelled")
    for offer in db.scalars(
        select(JobOffer).where(JobOffer.job_id == job.id, JobOffer.status == OfferStatus.PENDING)
    ):
        offer.status = OfferStatus.EXPIRED
    db.commit()
    if job.provider_id:
        publish("job.cancelled", job_id=str(job.id), notify_id=str(job.provider_id))
    hub.broadcast(job.id, tracking_payload(db, job))
    return job


# ======================================================================================
# Reading and tracking
# ======================================================================================
def get_job(db: Session, job_id: uuid.UUID, viewer: User) -> tuple[HaulageJob, Transaction]:
    job = db.get(HaulageJob, job_id)
    if job is None or not (
        viewer.id in (job.customer_id, job.provider_id) or viewer.has_role(*ADMIN_ROLES)
    ):
        raise NotFound("Job not found", code="job_not_found")
    return job, payments.get_transaction(db, job.transaction_id)


def tracking_payload(db: Session, job: HaulageJob) -> dict:
    """Everything a map screen needs: status, the provider's position and an ETA."""
    tx = payments.get_transaction(db, job.transaction_id)
    target = None  # where the provider is heading
    if tx.status in (TxStatus.CONFIRMED, TxStatus.EN_ROUTE):
        target = (job.pickup_lat, job.pickup_lng)
    elif tx.status == TxStatus.IN_TRANSIT:
        target = (job.drop_lat, job.drop_lng)
    remaining_km = eta_minutes = None
    if target and job.last_lat is not None:
        metres = (
            db.scalar(select(func.ST_Distance(_point(job.last_lat, job.last_lng), _point(*target))))
            or 0
        )
        remaining_km = round(metres / 1000 * ROAD_FACTOR, 1)
        eta_minutes = max(1, round(remaining_km / AVERAGE_SPEED_KMH * 60))
    return {
        "job_id": str(job.id),
        "status": tx.status.value,
        "provider_location": (
            {
                "latitude": job.last_lat,
                "longitude": job.last_lng,
                "at": job.last_location_at.isoformat(),
            }
            if job.last_lat is not None and job.last_location_at
            else None
        ),
        "remaining_km": remaining_km,
        "eta_minutes": eta_minutes,
    }


def user_may_watch(db: Session, job_id: uuid.UUID, user: User) -> bool:
    job = db.get(HaulageJob, job_id)
    return job is not None and (
        user.id in (job.customer_id, job.provider_id) or user.has_role(*ADMIN_ROLES)
    )


def sos_board(db: Session) -> list[dict]:
    """Admin 'Live SOS haulage board': open SOS jobs, longest-waiting first."""
    open_statuses = [s for s in TxStatus if s not in FINAL_STATUSES and s != TxStatus.DELIVERED]
    rows = db.execute(
        select(HaulageJob, Transaction)
        .join(Transaction, Transaction.id == HaulageJob.transaction_id)
        .where(
            HaulageJob.is_sos.is_(True),
            Transaction.status.in_(open_statuses),
            Transaction.paid_at.is_not(None),
        )
        .order_by(HaulageJob.created_at)
    ).all()
    names = (
        {
            u.id: u.display_name
            for u in db.scalars(
                select(User).where(
                    User.id.in_(
                        {j.customer_id for j, _ in rows}
                        | {j.provider_id for j, _ in rows if j.provider_id}
                    )
                )
            )
        }
        if rows
        else {}
    )
    now = utcnow()
    return [
        {
            "job_id": j.id, "ref": tx.ref, "status": tx.status,
            "customer": names.get(j.customer_id, ""), "provider": names.get(j.provider_id) if j.provider_id else None,
            "pickup_address": j.pickup_address, "waiting_minutes": int((now - j.created_at).total_seconds() // 60),
            "fare_minor": j.fare_minor,
        }
        for j, tx in rows
    ]  # fmt: skip
