"""Catalog business logic: garage, listings, search, inquiries and listing review.

Section map:
    1. Garage            - vehicles and service history (FSD 3.2)
    2. Listing rules     - validation, fraud signals, visibility
    3. Seller actions    - create, edit, images, status
    4. Search            - public browse and the mechanic directory
    5. Inquiries         - questions and offers (FR-3.5)
    6. Admin review      - the listing approval queue (FR-13.2)
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import date

from geoalchemy2 import WKTElement
from sqlalchemy import Integer, cast, func, or_, select
from sqlalchemy.orm import Session

from app.core import audit
from app.core.audit import AuditCategory
from app.core.db import utcnow
from app.core.errors import BadRequest, Conflict, Forbidden, NotFound, UnprocessableEntity
from app.core.events import publish
from app.core.pagination import paginate_page
from app.core.permissions import ADMIN_ROLES, Capability, Role, has_capability
from app.core.uploads import checked_type
from app.integrations import registry
from app.integrations.base import IntegrationError, VinInfo
from app.modules.catalog.models import (
    PUBLIC_STATUSES,
    InquiryStatus,
    Listing,
    ListingImage,
    ListingInquiry,
    ListingStatus,
    ListingType,
    ServiceRecord,
    Vehicle,
)
from app.modules.identity.models import KycStatus, User, UserRole
from app.modules.reviews import service as reviews

# ======================================================================================
# 1. Garage
# ======================================================================================
VIN_PATTERN = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")  # 17 characters, never I, O or Q


def normalise_vin(vin: str) -> str:
    vin = vin.strip().upper()
    if not VIN_PATTERN.match(vin):
        raise BadRequest("A VIN has 17 letters and digits (no I, O or Q)", code="invalid_vin")
    return vin


def decode_vin(vin: str) -> VinInfo:
    """Look up make/model/year for a VIN (FR-2.2). The driver is mock until a decoder is set."""
    vin = normalise_vin(vin)
    try:
        info = registry.get_vin().decode(vin)
    except IntegrationError as exc:
        raise UnprocessableEntity(
            "The VIN lookup service is unavailable", code="vin_unavailable"
        ) from exc
    if info is None:
        raise NotFound("We could not decode that VIN", code="vin_not_found")
    return info


def list_vehicles(db: Session, owner: User) -> list[Vehicle]:
    return list(
        db.scalars(
            select(Vehicle)
            .where(Vehicle.owner_id == owner.id, Vehicle.deleted_at.is_(None))
            .order_by(Vehicle.created_at.desc())
        )
    )


def add_vehicle(db: Session, owner: User, data: dict) -> Vehicle:
    if data.get("vin"):
        data["vin"] = normalise_vin(data["vin"])
        taken = db.scalar(
            select(Vehicle.id).where(
                Vehicle.owner_id == owner.id,
                Vehicle.vin == data["vin"],
                Vehicle.deleted_at.is_(None),
            )
        )
        if taken:
            raise Conflict("This vehicle is already in your garage", code="vehicle_exists")
    vehicle = Vehicle(owner_id=owner.id, **data)
    db.add(vehicle)
    db.commit()
    return vehicle


def get_vehicle(db: Session, owner: User, vehicle_id: uuid.UUID) -> Vehicle:
    vehicle = db.get(Vehicle, vehicle_id)
    if vehicle is None or vehicle.deleted_at or vehicle.owner_id != owner.id:
        raise NotFound("Vehicle not found", code="vehicle_not_found")
    return vehicle


def update_vehicle(db: Session, owner: User, vehicle_id: uuid.UUID, changes: dict) -> Vehicle:
    vehicle = get_vehicle(db, owner, vehicle_id)
    if changes.get("vin"):
        changes["vin"] = normalise_vin(changes["vin"])
    for field, value in changes.items():
        setattr(vehicle, field, value)
    db.commit()
    return vehicle


def delete_vehicle(db: Session, owner: User, vehicle_id: uuid.UUID) -> None:
    """Soft delete: service history of a removed vehicle is kept for audit."""
    get_vehicle(db, owner, vehicle_id).deleted_at = utcnow()
    db.commit()


def add_service_record(
    db: Session, vehicle: Vehicle, data: dict, *, commit: bool = True
) -> ServiceRecord:
    """Used by the owner (manual entry) and by the booking module (automatic entry)."""
    if data["done_on"] > date.today():
        raise BadRequest("The service date cannot be in the future", code="invalid_date")
    record = ServiceRecord(vehicle_id=vehicle.id, **data)
    db.add(record)
    if data.get("mileage_km") and (vehicle.mileage_km or 0) < data["mileage_km"]:
        vehicle.mileage_km = data["mileage_km"]  # keep the odometer reading current
    if commit:
        db.commit()
    else:
        db.flush()
    return record


# ======================================================================================
# 2. Listing rules
# ======================================================================================
CAPABILITY_FOR_TYPE = {
    ListingType.VEHICLE: Capability.LIST_VEHICLES,
    ListingType.PART: Capability.LIST_PARTS,
    ListingType.MECHANIC_SERVICE: Capability.OFFER_SERVICES,
    ListingType.INSURANCE_PRODUCT: Capability.OFFER_INSURANCE,
}

# Keys that must be present in `attributes` for each type.
REQUIRED_ATTRIBUTES = {
    ListingType.VEHICLE: ("make", "model", "year", "mileage_km"),
    ListingType.PART: ("brand", "condition"),
    ListingType.MECHANIC_SERVICE: ("category",),
    ListingType.INSURANCE_PRODUCT: ("cover_type",),
}

# Phone numbers, e-mail addresses and "contact me" phrases in a description are a classic
# way to take a deal off the platform (and out of escrow), so they raise a fraud signal.
_CONTACT_PATTERN = re.compile(
    r"(\+?\d[\d\s\-]{8,}\d)|([\w.+-]+@[\w-]+\.[\w.]+)|whats\s?app|call me|text me",
    re.IGNORECASE,
)


def _check_attributes(type_: ListingType, attributes: dict) -> None:
    missing = [k for k in REQUIRED_ATTRIBUTES[type_] if attributes.get(k) in (None, "")]
    if missing:
        raise UnprocessableEntity(
            f"Missing details for a {type_.value.replace('_', ' ')}: {', '.join(missing)}",
            code="missing_attributes",
            details={"missing": missing},
        )


def _set_location(listing: Listing, latitude: float | None, longitude: float | None) -> None:
    """Keep the plain coordinates and the PostGIS point in step."""
    listing.latitude, listing.longitude = latitude, longitude
    listing.location = (
        WKTElement(f"POINT({longitude} {latitude})", srid=4326)
        if latitude is not None and longitude is not None
        else None
    )


def scan_fraud_signals(db: Session, listing: Listing) -> list[str]:
    """Automatic checks shown to the reviewing admin (FR-13.2).

    * duplicate_vin     - another active listing has the same VIN
    * contact_details   - phone or e-mail written into the title or description
    * duplicate_photos  - a photo is identical to one on another seller's listing
    """
    signals: list[str] = []
    active = [s for s in ListingStatus if s not in (ListingStatus.REJECTED, ListingStatus.REMOVED)]
    if listing.vin and db.scalar(
        select(Listing.id).where(
            Listing.vin == listing.vin, Listing.id != listing.id, Listing.status.in_(active)
        )
    ):
        signals.append("duplicate_vin")
    if _CONTACT_PATTERN.search(f"{listing.title} {listing.description}"):
        signals.append("contact_details")
    hashes = [i.sha256 for i in listing.images]
    if hashes and db.scalar(
        select(ListingImage.id)
        .join(Listing, Listing.id == ListingImage.listing_id)
        .where(
            ListingImage.sha256.in_(hashes),
            Listing.seller_id != listing.seller_id,
            Listing.status.in_(active),
        )
    ):
        signals.append("duplicate_photos")
    return signals


def _refresh_signals(db: Session, listing: Listing) -> None:
    """Re-scan. A live listing that picks up a new warning goes back to review."""
    db.flush()
    before = set(listing.fraud_signals or [])
    listing.fraud_signals = scan_fraud_signals(db, listing)
    if listing.status == ListingStatus.LIVE and set(listing.fraud_signals) - before:
        listing.status = ListingStatus.PENDING


def _can_view_all(user: User | None) -> bool:
    return bool(user and user.has_role(*ADMIN_ROLES))


def get_listing(db: Session, listing_id: uuid.UUID, viewer: User | None) -> Listing:
    """Fetch for display. Non-public listings are visible only to their seller and staff."""
    listing = db.get(Listing, listing_id)
    if listing is None or listing.status == ListingStatus.REMOVED:
        raise NotFound("Listing not found", code="listing_not_found")
    is_owner = viewer is not None and viewer.id == listing.seller_id
    if listing.status not in PUBLIC_STATUSES and not (is_owner or _can_view_all(viewer)):
        raise NotFound("Listing not found", code="listing_not_found")
    return listing


def _get_own(db: Session, user: User, listing_id: uuid.UUID) -> Listing:
    listing = db.get(Listing, listing_id)
    if listing is None or listing.status == ListingStatus.REMOVED or listing.seller_id != user.id:
        raise NotFound("Listing not found", code="listing_not_found")
    return listing


# ======================================================================================
# 3. Seller actions
# ======================================================================================
def create_listing(db: Session, seller: User, data: dict) -> Listing:
    """Create a listing. It starts as `pending` and goes live when an admin approves it."""
    type_ = data["type"]
    if not has_capability(seller.active_roles, CAPABILITY_FOR_TYPE[type_]):
        raise Forbidden("Your account type cannot create this kind of listing", code="forbidden")
    _check_attributes(type_, data["attributes"])
    if data.get("vin"):
        data["vin"] = normalise_vin(data["vin"])
    latitude, longitude = data.pop("latitude", None), data.pop("longitude", None)
    listing = Listing(seller_id=seller.id, city=data.pop("city", None) or seller.city, **data)
    _set_location(listing, latitude, longitude)
    db.add(listing)
    _refresh_signals(db, listing)
    db.commit()
    publish("listing.submitted", listing_id=str(listing.id), ref=listing.ref)
    return listing


def update_listing(db: Session, seller: User, listing_id: uuid.UUID, changes: dict) -> Listing:
    listing = _get_own(db, seller, listing_id)
    if listing.status in (ListingStatus.SOLD, ListingStatus.SUSPENDED):
        raise UnprocessableEntity("This listing can no longer be edited", code="not_editable")
    if "attributes" in changes:
        _check_attributes(listing.type, changes["attributes"])
    if changes.get("vin"):
        changes["vin"] = normalise_vin(changes["vin"])
    if "latitude" in changes or "longitude" in changes:
        _set_location(
            listing,
            changes.pop("latitude", listing.latitude),
            changes.pop("longitude", listing.longitude),
        )
    for field, value in changes.items():
        setattr(listing, field, value)
    if listing.status == ListingStatus.REJECTED:
        listing.status, listing.rejection_reason = ListingStatus.PENDING, None  # resubmit
    _refresh_signals(db, listing)
    db.commit()
    return listing


SELLER_STATUS_MOVES = {
    ListingStatus.LIVE: {ListingStatus.RESERVED, ListingStatus.SOLD},
    ListingStatus.RESERVED: {ListingStatus.LIVE, ListingStatus.SOLD},
    ListingStatus.SOLD: {ListingStatus.LIVE},  # relisted / back in stock
}


def set_seller_status(
    db: Session, seller: User, listing_id: uuid.UUID, status: ListingStatus
) -> Listing:
    """Sellers move a live listing between live / reserved / sold. Review is admin-only."""
    listing = _get_own(db, seller, listing_id)
    if status not in SELLER_STATUS_MOVES.get(listing.status, set()):
        raise UnprocessableEntity(
            f"A {listing.status.value} listing cannot become {status.value}", code="invalid_status"
        )
    listing.status = status
    db.commit()
    return listing


def remove_listing(db: Session, seller: User, listing_id: uuid.UUID) -> None:
    _get_own(db, seller, listing_id).status = ListingStatus.REMOVED
    db.commit()


IMAGE_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
IMAGE_MAX_BYTES = 8 * 1024 * 1024
MAX_IMAGES = 12


def add_image(
    db: Session, seller: User, listing_id: uuid.UUID, content_type: str, data: bytes
) -> ListingImage:
    listing = _get_own(db, seller, listing_id)
    content_type = checked_type(
        data, IMAGE_TYPES, message="Photos must be JPEG, PNG or WebP", code="invalid_file_type"
    )
    if not data or len(data) > IMAGE_MAX_BYTES:
        raise BadRequest("Photos must be under 8 MB", code="file_too_large")
    if len(listing.images) >= MAX_IMAGES:
        raise UnprocessableEntity(f"A listing can have up to {MAX_IMAGES} photos", code="too_many")
    key = f"listings/{listing.id}/{uuid.uuid4().hex}{IMAGE_TYPES[content_type]}"
    registry.get_storage().put(key, data, content_type)
    image = ListingImage(
        listing_id=listing.id,
        storage_key=key,
        sha256=hashlib.sha256(data).hexdigest(),
        position=len(listing.images),
    )
    listing.images.append(image)
    _refresh_signals(db, listing)
    db.commit()
    return image


def delete_image(db: Session, seller: User, listing_id: uuid.UUID, image_id: uuid.UUID) -> None:
    listing = _get_own(db, seller, listing_id)
    image = next((i for i in listing.images if i.id == image_id), None)
    if image is None:
        raise NotFound("Photo not found", code="image_not_found")
    registry.get_storage().delete(image.storage_key)
    listing.images.remove(image)
    _refresh_signals(db, listing)
    db.commit()


def image_url(image: ListingImage) -> str:
    """A time-limited link the app can show (private storage, so never a permanent URL)."""
    return registry.get_storage().url(image.storage_key)


def my_listings(
    db: Session, seller: User, status: ListingStatus | None, page: int, page_size: int
) -> tuple[list[Listing], int]:
    stmt = select(Listing).where(
        Listing.seller_id == seller.id, Listing.status != ListingStatus.REMOVED
    )
    if status:
        stmt = stmt.where(Listing.status == status)
    return paginate_page(
        db, stmt.order_by(Listing.created_at.desc()), page=page, page_size=page_size
    )


# ======================================================================================
# 4. Search
# ======================================================================================
def search_listings(
    db: Session,
    *,
    q: str | None = None,
    type_: ListingType | None = None,
    city: str | None = None,
    min_price: int | None = None,
    max_price: int | None = None,
    make: str | None = None,
    year_min: int | None = None,
    year_max: int | None = None,
    seller_id: uuid.UUID | None = None,
    near: tuple[float, float] | None = None,  # (latitude, longitude)
    radius_km: float = 25,
    sort: str = "newest",
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[Listing], int, dict[str, int]]:
    """Public search across all four listing types. Returns (rows, total, count per type).

    Uses simple case-insensitive matching (backed by the trigram extension on bigger data).
    With `near`, only listings within `radius_km` are returned (PostGIS) and the distance
    sort becomes available.
    """
    stmt = select(Listing).where(Listing.status.in_([ListingStatus.LIVE, ListingStatus.RESERVED]))
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Listing.title.ilike(like), Listing.description.ilike(like)))
    if city:
        stmt = stmt.where(Listing.city.ilike(city.strip()))
    if min_price is not None:
        stmt = stmt.where(Listing.price_minor >= min_price)
    if max_price is not None:
        stmt = stmt.where(Listing.price_minor <= max_price)
    if seller_id:
        stmt = stmt.where(Listing.seller_id == seller_id)
    if make:
        stmt = stmt.where(Listing.attributes["make"].astext.ilike(make.strip()))
    year = cast(Listing.attributes["year"].astext, Integer)
    if year_min is not None:
        stmt = stmt.where(year >= year_min)
    if year_max is not None:
        stmt = stmt.where(year <= year_max)

    # Counts per type use every filter except the type itself, so the app can show tabs.
    counts = _counts_by_type(db, stmt)
    if type_:
        stmt = stmt.where(Listing.type == type_)

    distance = None
    if near:
        point = func.ST_GeogFromText(f"SRID=4326;POINT({near[1]} {near[0]})")
        distance = func.ST_Distance(Listing.location, point)
        stmt = stmt.where(Listing.location.is_not(None)).where(
            func.ST_DWithin(Listing.location, point, radius_km * 1000)
        )

    if sort == "price_asc":
        stmt = stmt.order_by(Listing.price_minor.asc(), Listing.id)
    elif sort == "price_desc":
        stmt = stmt.order_by(Listing.price_minor.desc(), Listing.id)
    elif sort == "distance" and distance is not None:
        stmt = stmt.order_by(distance.asc(), Listing.id)
    else:
        stmt = stmt.order_by(Listing.created_at.desc(), Listing.id)
    rows, total = paginate_page(db, stmt, page=page, page_size=page_size)
    return rows, total, counts


def _counts_by_type(db: Session, filtered) -> dict[str, int]:
    sub = filtered.subquery()
    rows = db.execute(select(sub.c.type, func.count()).group_by(sub.c.type)).all()
    counts = {t.value: 0 for t in ListingType}
    counts.update({str(t): n for t, n in rows})
    return counts


def mechanic_directory(
    db: Session,
    *,
    city: str | None,
    near: tuple[float, float] | None,
    radius_km: float,
    page: int,
    page_size: int,
) -> tuple[list[dict], int]:
    """Verified mechanics with at least one live service, best rated first (FR-4.1).

    Returns plain dicts: user, rating, starting price and number of services.
    """
    live_services = select(Listing).where(
        Listing.type == ListingType.MECHANIC_SERVICE, Listing.status == ListingStatus.LIVE
    )
    if city:
        live_services = live_services.where(Listing.city.ilike(city.strip()))
    if near:
        point = func.ST_GeogFromText(f"SRID=4326;POINT({near[1]} {near[0]})")
        live_services = live_services.where(Listing.location.is_not(None)).where(
            func.ST_DWithin(Listing.location, point, radius_km * 1000)
        )
    sub = live_services.subquery()
    stmt = (
        select(User, func.min(sub.c.price_minor), func.count(sub.c.id))
        .join(sub, sub.c.seller_id == User.id)
        .join(UserRole, (UserRole.user_id == User.id) & (UserRole.role == Role.MECHANIC))
        .where(UserRole.active.is_(True), User.kyc_status == KycStatus.APPROVED)
        .group_by(User.id)
        .order_by(User.created_at)
    )
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    rows = db.execute(stmt.limit(page_size).offset((max(page, 1) - 1) * page_size)).all()
    ratings = reviews.ratings_for(db, [u.id for u, _, _ in rows])
    items = [
        {
            "user": u,
            "rating": ratings.get(u.id, (0.0, 0))[0],
            "review_count": ratings.get(u.id, (0.0, 0))[1],
            "from_price_minor": price,
            "services": n,
        }
        for u, price, n in rows
    ]
    items.sort(key=lambda i: (-i["rating"], -i["review_count"]))
    return items, total


# ======================================================================================
# 5. Inquiries
# ======================================================================================
def create_inquiry(
    db: Session, buyer: User, listing_id: uuid.UUID, message: str, offer_minor: int | None
) -> ListingInquiry:
    listing = get_listing(db, listing_id, buyer)
    if listing.seller_id == buyer.id:
        raise UnprocessableEntity("You cannot message your own listing", code="own_listing")
    if listing.status != ListingStatus.LIVE:
        raise UnprocessableEntity("This listing is not accepting enquiries", code="not_live")
    inquiry = ListingInquiry(
        listing_id=listing.id, buyer_id=buyer.id, message=message.strip(), offer_minor=offer_minor
    )
    db.add(inquiry)
    db.commit()
    publish("inquiry.created", inquiry_id=str(inquiry.id), listing_id=str(listing.id))
    return inquiry


def list_inquiries(db: Session, seller: User, listing_id: uuid.UUID) -> list[ListingInquiry]:
    listing = _get_own(db, seller, listing_id)
    return list(
        db.scalars(
            select(ListingInquiry)
            .where(ListingInquiry.listing_id == listing.id)
            .order_by(ListingInquiry.created_at.desc())
        )
    )


def respond_to_inquiry(
    db: Session, seller: User, inquiry_id: uuid.UUID, accept: bool
) -> ListingInquiry:
    inquiry = db.get(ListingInquiry, inquiry_id)
    listing = db.get(Listing, inquiry.listing_id) if inquiry else None
    if inquiry is None or listing is None or listing.seller_id != seller.id:
        raise NotFound("Enquiry not found", code="inquiry_not_found")
    if inquiry.status != InquiryStatus.OPEN:
        raise UnprocessableEntity("This enquiry was already answered", code="already_answered")
    inquiry.status = InquiryStatus.ACCEPTED if accept else InquiryStatus.DECLINED
    db.commit()
    publish(
        "inquiry.answered",
        inquiry_id=str(inquiry.id),
        buyer_id=str(inquiry.buyer_id),
        accepted=accept,
    )
    return inquiry


# ======================================================================================
# 6. Admin review
# ======================================================================================
def review_queue(
    db: Session, *, status: ListingStatus | None, flagged: bool, page: int, page_size: int
) -> tuple[list[Listing], int]:
    """The 'Listing review' screen: pending by default, oldest first. `flagged` shows only
    listings with fraud signals."""
    stmt = select(Listing).where(Listing.status == (status or ListingStatus.PENDING))
    if flagged:
        stmt = stmt.where(func.jsonb_array_length(Listing.fraud_signals) > 0)
    return paginate_page(db, stmt.order_by(Listing.created_at), page=page, page_size=page_size)


def review_counts(db: Session) -> dict[str, int]:
    pending = (
        select(func.count()).select_from(Listing).where(Listing.status == ListingStatus.PENDING)
    )
    return {
        "pending": db.scalar(pending) or 0,
        "flagged": db.scalar(pending.where(func.jsonb_array_length(Listing.fraud_signals) > 0))
        or 0,
    }


def decide_listing(
    db: Session,
    listing_id: uuid.UUID,
    *,
    approve: bool,
    reason: str | None,
    admin: User,
    ip: str | None,
) -> Listing:
    """Approve (go live) or reject (with a reason the seller sees)."""
    listing = db.get(Listing, listing_id)
    if listing is None or listing.status == ListingStatus.REMOVED:
        raise NotFound("Listing not found", code="listing_not_found")
    if listing.status != ListingStatus.PENDING:
        raise UnprocessableEntity("Only pending listings can be reviewed", code="not_pending")
    seller = db.get(User, listing.seller_id)
    if approve:
        # Business sellers must be KYC-approved before anything of theirs goes live (FR-1.4).
        if seller.kyc_status in (KycStatus.NOT_SUBMITTED, KycStatus.PENDING, KycStatus.REJECTED):
            raise UnprocessableEntity(
                "The seller's identity verification is not approved yet", code="seller_not_verified"
            )
        listing.status, listing.rejection_reason = ListingStatus.LIVE, None
    else:
        if not reason or not reason.strip():
            raise BadRequest("Give the seller a reason for rejecting", code="reason_required")
        listing.status, listing.rejection_reason = ListingStatus.REJECTED, reason.strip()[:300]
    listing.reviewed_by_id, listing.reviewed_at = admin.id, utcnow()
    audit.record(
        db,
        category=AuditCategory.ADMIN,
        action="Approved listing" if approve else "Rejected listing",
        actor_id=admin.id,
        actor_name=admin.full_name,
        target=listing.ref,
        ip_address=ip,
    )
    db.commit()
    publish(
        "listing.approved" if approve else "listing.rejected",
        listing_id=str(listing.id),
        seller_id=str(listing.seller_id),
        ref=listing.ref,
    )
    return listing


def suspend_listing(
    db: Session, listing_id: uuid.UUID, *, reason: str, admin: User, ip: str | None
) -> Listing:
    """Take a live listing down (e.g. after an abuse report)."""
    listing = db.get(Listing, listing_id)
    if listing is None or listing.status in (ListingStatus.REMOVED, ListingStatus.SUSPENDED):
        raise NotFound("Listing not found", code="listing_not_found")
    listing.status, listing.rejection_reason = ListingStatus.SUSPENDED, reason.strip()[:300]
    audit.record(
        db,
        category=AuditCategory.ADMIN,
        action="Suspended listing",
        actor_id=admin.id,
        actor_name=admin.full_name,
        target=listing.ref,
        ip_address=ip,
    )
    db.commit()
    publish("listing.suspended", listing_id=str(listing.id), seller_id=str(listing.seller_id))
    return listing
