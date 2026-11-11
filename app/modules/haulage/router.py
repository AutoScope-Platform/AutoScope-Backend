"""Haulage endpoints (REST) and the live tracking WebSocket."""

from __future__ import annotations

import asyncio
import uuid

import jwt
from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.core import db as db_module
from app.core.db import get_db
from app.core.errors import error_responses
from app.core.permissions import Capability
from app.core.security import decode_token
from app.modules.haulage import hub, service
from app.modules.haulage.models import HaulageJob
from app.modules.haulage.schemas import (
    CancelIn,
    EstimateOut,
    JobIn,
    JobOut,
    OfferOut,
    PresenceIn,
    PresenceOut,
    SosJobOut,
    TrackingOut,
)
from app.modules.identity.deps import CurrentUser, require_admin, require_capability
from app.modules.identity.models import AccountStatus, User
from app.modules.payments import service as payments

router = APIRouter(tags=["Haulage"])

Customer = Depends(require_capability(Capability.PURCHASE))
Provider = Depends(require_capability(Capability.OFFER_HAULAGE))


def _out(db: Session, job: HaulageJob, tx=None) -> JobOut:
    tx = tx or payments.get_transaction(db, job.transaction_id)
    return JobOut(
        id=job.id, transaction_id=tx.id, ref=tx.ref, status=tx.status, escrow_status=tx.escrow_status,
        is_sos=job.is_sos, payment_due=tx.payment_due, customer_id=job.customer_id,
        provider_id=job.provider_id, vehicle_description=job.vehicle_description,
        pickup_address=job.pickup_address, drop_address=job.drop_address,
        distance_km=job.distance_km, fare_minor=job.fare_minor, created_at=job.created_at,
    )  # fmt: skip


# --- Customer ----------------------------------------------------------------------------
@router.get(
    "/haulage/estimate",
    response_model=EstimateOut,
    summary="Fare estimate for a tow",
    description="Distance uses PostGIS (straight line x 1.3). SOS costs 1.5x.",
)
def estimate(
    pickup_lat: float = Query(ge=-90, le=90),
    pickup_lng: float = Query(ge=-180, le=180),
    drop_lat: float = Query(ge=-90, le=90),
    drop_lng: float = Query(ge=-180, le=180),
    sos: bool = False,
    db: Session = Depends(get_db),
) -> EstimateOut:
    km, fare = service.estimate_fare(db, (pickup_lat, pickup_lng), (drop_lat, drop_lng), sos)
    return EstimateOut(distance_km=km, fare_minor=fare, sos=sos)


@router.post(
    "/haulage/jobs",
    response_model=JobOut,
    status_code=status.HTTP_201_CREATED,
    summary="Request a tow or SOS",
    description="Pay the fare with `POST /payments` using the returned `transaction_id`; "
    "dispatch to nearby providers starts when payment succeeds.",
    responses=error_responses(404, 422),
)
def request_job(body: JobIn, user: User = Customer, db: Session = Depends(get_db)):
    job = service.request_job(
        db, user,
        pickup=(body.pickup.latitude, body.pickup.longitude, body.pickup.address),
        drop=(body.drop.latitude, body.drop.longitude, body.drop.address),
        vehicle_id=body.vehicle_id, vehicle_description=body.vehicle_description,
        notes=body.notes, sos=body.sos,
    )  # fmt: skip
    return _out(db, job)


@router.get(
    "/haulage/jobs/{job_id}",
    response_model=JobOut,
    summary="One job (customer, provider or staff)",
    responses=error_responses(404),
)
def get_job(job_id: uuid.UUID, user: CurrentUser, db: Session = Depends(get_db)):
    job, tx = service.get_job(db, job_id, user)
    return _out(db, job, tx)


@router.get(
    "/haulage/jobs/{job_id}/tracking",
    response_model=TrackingOut,
    summary="Provider position and ETA (poll this, or use the WebSocket)",
    responses=error_responses(404),
)
def tracking(job_id: uuid.UUID, user: CurrentUser, db: Session = Depends(get_db)):
    job, _ = service.get_job(db, job_id, user)
    return service.tracking_payload(db, job)


@router.post(
    "/haulage/jobs/{job_id}/cancel",
    response_model=JobOut,
    summary="Cancel up to pickup (paid jobs are refunded)",
    responses=error_responses(404, 422),
)
def cancel(job_id: uuid.UUID, body: CancelIn, user: User = Customer, db: Session = Depends(get_db)):
    return _out(db, service.cancel_job(db, user, job_id, body.reason))


# --- Provider ----------------------------------------------------------------------------
@router.put(
    "/me/haulage/presence",
    response_model=PresenceOut,
    summary="Provider: go online/offline and report my position",
    description="Send this regularly while online. Only providers who are online and have "
    "reported a position in the last 30 minutes are offered jobs.",
)
def presence(body: PresenceIn, user: User = Provider, db: Session = Depends(get_db)):
    return service.set_presence(
        db, user, available=body.available, latitude=body.latitude, longitude=body.longitude
    )


@router.get(
    "/me/haulage/offers",
    response_model=list[OfferOut],
    summary="Provider: jobs currently offered to me (SOS first)",
)
def offers(user: User = Provider, db: Session = Depends(get_db)) -> list[OfferOut]:
    return [
        OfferOut(
            offer_id=o.id, job_id=j.id, is_sos=j.is_sos, distance_to_pickup_m=o.distance_m,
            pickup_address=j.pickup_address, drop_address=j.drop_address, fare_minor=j.fare_minor,
            expires_at=o.expires_at,
        )
        for o, j in service.list_offers(db, user)
    ]  # fmt: skip


@router.post(
    "/haulage/offers/{offer_id}/accept",
    response_model=JobOut,
    summary="Provider: accept an offered job (first to accept gets it)",
    responses=error_responses(404, 409, 422),
)
def accept(offer_id: uuid.UUID, user: User = Provider, db: Session = Depends(get_db)):
    return _out(db, service.accept_offer(db, user, offer_id))


@router.post(
    "/haulage/offers/{offer_id}/decline",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Provider: decline an offered job",
    responses=error_responses(404),
)
def decline(offer_id: uuid.UUID, user: User = Provider, db: Session = Depends(get_db)) -> None:
    service.decline_offer(db, user, offer_id)


@router.post(
    "/haulage/jobs/{job_id}/start",
    response_model=JobOut,
    summary="Provider: heading to the pickup point",
    responses=error_responses(404, 422),
)
def start(job_id: uuid.UUID, user: User = Provider, db: Session = Depends(get_db)):
    return _out(db, service.start_trip(db, user, job_id))


@router.post(
    "/haulage/jobs/{job_id}/pickup",
    response_model=JobOut,
    summary="Provider: vehicle loaded, heading to the drop-off",
    responses=error_responses(404, 422),
)
def pickup(job_id: uuid.UUID, user: User = Provider, db: Session = Depends(get_db)):
    return _out(db, service.pick_up(db, user, job_id))


@router.post(
    "/haulage/jobs/{job_id}/complete",
    response_model=JobOut,
    summary="Provider: delivered",
    responses=error_responses(404, 422),
)
def complete(job_id: uuid.UUID, user: User = Provider, db: Session = Depends(get_db)):
    return _out(db, service.complete_job(db, user, job_id))


# --- Admin -------------------------------------------------------------------------------
@router.get(
    "/admin/haulage/sos",
    response_model=list[SosJobOut],
    summary="Live SOS board (admin)",
    description="Open, paid SOS jobs, longest waiting first.",
    dependencies=[Depends(require_admin)],
)
def sos_board(db: Session = Depends(get_db)):
    return service.sos_board(db)


# --- WebSocket ---------------------------------------------------------------------------
def _authorise_watcher(job_id: uuid.UUID, token: str) -> dict | None:
    """Blocking helper (runs in a thread): the token's user may watch the job -> snapshot."""
    try:
        user_id = uuid.UUID(decode_token(token, expected_type="access")["sub"])
    except (jwt.PyJWTError, ValueError, KeyError):
        return None
    db = db_module.new_session()
    try:
        user = db.get(User, user_id)
        if user is None or user.status == AccountStatus.SUSPENDED or user.erased_at is not None:
            return None
        if not service.user_may_watch(db, job_id, user):
            return None
        return service.tracking_payload(db, db.get(HaulageJob, job_id))
    finally:
        db.close()


@router.websocket("/ws/jobs/{job_id}/track")
async def track(websocket: WebSocket, job_id: uuid.UUID, token: str = Query("")) -> None:
    """Live tracking. Connect with `?token=<access token>`. The server sends the current
    state at once, then a message whenever the status or provider position changes.

    Browsers cannot set headers on WebSockets, which is why the token goes in the query string.
    Messages have the same shape as `GET /haulage/jobs/{id}/tracking`.
    """
    snapshot = await run_in_threadpool(_authorise_watcher, job_id, token)
    if snapshot is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await websocket.accept()
    queue = hub.subscribe(job_id)
    try:
        await websocket.send_json(snapshot)
        receiver = asyncio.create_task(websocket.receive_text())  # notices the client leaving
        while True:
            getter = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait({getter, receiver}, return_when=asyncio.FIRST_COMPLETED)
            if receiver in done:
                getter.cancel()
                receiver.result()  # raises WebSocketDisconnect when the client closed
                receiver = asyncio.create_task(websocket.receive_text())  # ignore chatter
            if getter in done:
                await websocket.send_json(getter.result())
    except WebSocketDisconnect:
        pass
    finally:
        hub.unsubscribe(job_id, queue)
