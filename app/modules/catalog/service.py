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
from datetime import date, datetime

from geoalchemy2 import WKTElement
from sqlalchemy import Float, Integer, cast, func, null, or_, select, update
from sqlalchemy.orm import Session, selectinload

from app.core import audit
from app.core import db as db_module
from app.core.audit import AuditCategory
from app.core.db import utcnow
from app.core.errors import (
    BadRequest,
    Conflict,
    Forbidden,
    NotFound,
    Unauthorized,
    UnprocessableEntity,
)
from app.core.events import Event, publish, publish_after_commit, subscribe
from app.core.pagination import paginate_page
from app.core.permissions import ADMIN_ROLES, Capability, Role, has_capability
from app.core.uploads import checked_type
from app.integrations import registry
from app.integrations.base import IntegrationError, VinInfo
from app.modules.catalog.models import (
    PUBLIC_STATUSES,
    VIEWING_KINDS,
    InquiryKind,
    InquiryStatus,
    Listing,
    ListingImage,
    ListingInquiry,
    ListingStatus,
    ListingType,
    PartFitment,
    PartQuote,
    PartRequest,
    PartRequestStatus,
    ServiceRecord,
    Vehicle,
)
from app.modules.identity.models import KycStatus, User, UserRole
from app.modules.payments.models import Transaction, TransactionKind
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


def _check_part_fields(type_: ListingType, sku: str | None, fits: list | None) -> None:
    if type_ != ListingType.PART and (sku or fits):
        raise UnprocessableEntity(
            "A SKU and vehicle fitment can only be given for parts", code="parts_only"
        )


def _clean_sku(sku: str | None) -> str | None:
    return (sku or "").strip() or None


def _check_sku_free(
    db: Session, seller_id: uuid.UUID, sku: str | None, listing_id: uuid.UUID | None = None
) -> None:
    """A seller's SKUs identify their stock, so two active listings cannot share one."""
    if not sku:
        return
    stmt = select(Listing.id).where(
        Listing.seller_id == seller_id,
        func.lower(Listing.sku) == sku.lower(),
        Listing.status != ListingStatus.REMOVED,
    )
    if listing_id:
        stmt = stmt.where(Listing.id != listing_id)
    if db.scalar(stmt):
        raise Conflict("You already have a listing with this SKU", code="duplicate_sku")


def _set_fitments(listing: Listing, fits: list[dict]) -> None:
    listing.fitments.clear()
    for f in fits:
        listing.fitments.append(
            PartFitment(
                make=f["make"].strip(),
                model=(f.get("model") or "").strip() or None,
                year_from=f.get("year_from"),
                year_to=f.get("year_to"),
            )
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


def view_listing(db: Session, listing_id: uuid.UUID, viewer: User | None) -> Listing:
    """`get_listing` for the listing page: also counts the view (FR-3.5) unless the viewer
    is the seller or staff. The counter is bumped in SQL, so concurrent views all count."""
    listing = get_listing(db, listing_id, viewer)
    if listing.status in PUBLIC_STATUSES and not (
        (viewer is not None and viewer.id == listing.seller_id) or _can_view_all(viewer)
    ):
        db.execute(
            update(Listing)
            .where(Listing.id == listing.id)
            .values(view_count=Listing.view_count + 1)
        )
        db.commit()
        db.refresh(listing)
    return listing


def listing_stats(db: Session, listings: list[Listing]) -> dict[uuid.UUID, dict]:
    """Views and enquiries per listing, for the seller's dashboard (FR-3.5)."""
    if not listings:
        return {}
    rows = db.execute(
        select(ListingInquiry.listing_id, ListingInquiry.kind, func.count())
        .where(ListingInquiry.listing_id.in_([li.id for li in listings]))
        .group_by(ListingInquiry.listing_id, ListingInquiry.kind)
    ).all()
    stats = {
        li.id: {"views": li.view_count or 0, "enquiries": 0, "offers": 0, "viewing_requests": 0}
        for li in listings
    }
    for listing_id, kind, n in rows:
        stats[listing_id]["enquiries"] += n
        if kind == InquiryKind.OFFER:
            stats[listing_id]["offers"] += n
        elif kind in VIEWING_KINDS:
            stats[listing_id]["viewing_requests"] += n
    return stats


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
    fits = data.pop("fits", None) or []
    data["sku"] = _clean_sku(data.pop("sku", None))
    _check_part_fields(type_, data["sku"], fits)
    _check_sku_free(db, seller.id, data["sku"])
    if data.get("vin"):
        data["vin"] = normalise_vin(data["vin"])
    latitude, longitude = data.pop("latitude", None), data.pop("longitude", None)
    listing = Listing(seller_id=seller.id, city=data.pop("city", None) or seller.city, **data)
    _set_fitments(listing, fits)
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
    if "sku" in changes:
        changes["sku"] = _clean_sku(changes["sku"])
    _check_part_fields(listing.type, changes.get("sku"), changes.get("fits"))
    if changes.get("sku"):
        _check_sku_free(db, seller.id, changes["sku"], listing.id)
    if "fits" in changes:
        _set_fitments(listing, changes.pop("fits") or [])
    stock_changed = "stock" in changes or "low_stock_threshold" in changes
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
    if stock_changed:
        note_stock_change(db, listing)
    _refresh_signals(db, listing)
    db.commit()
    return listing


def note_stock_change(db: Session, listing: Listing) -> None:
    """Low-stock alert (FR-4.5): tell the seller once when stock falls to the threshold;
    re-arm when it is back above. Sent after the caller commits."""
    threshold = listing.low_stock_threshold
    if threshold is None or listing.stock is None:
        return
    if listing.stock <= threshold and listing.low_stock_alerted_at is None:
        listing.low_stock_alerted_at = utcnow()
        publish_after_commit(
            db, "listing.low_stock", listing_id=str(listing.id), seller_id=str(listing.seller_id),
            title=listing.title, stock=listing.stock,
        )  # fmt: skip
    elif listing.stock > threshold:
        listing.low_stock_alerted_at = None


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


VIDEO_TYPES = {"video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm"}
VIDEO_MAX_BYTES = 50 * 1024 * 1024


def set_video(db: Session, seller: User, listing_id: uuid.UUID, data: bytes) -> Listing:
    """Attach (or replace) the listing's one video (FR-3.1): MP4, MOV or WebM up to 50 MB."""
    listing = _get_own(db, seller, listing_id)
    content_type = checked_type(
        data, VIDEO_TYPES, message="Videos must be MP4, MOV or WebM", code="invalid_file_type"
    )
    if len(data) > VIDEO_MAX_BYTES:
        raise BadRequest("Videos must be under 50 MB", code="file_too_large")
    storage = registry.get_storage()
    key = f"listings/{listing.id}/video-{uuid.uuid4().hex}{VIDEO_TYPES[content_type]}"
    storage.put(key, data, content_type)
    if listing.video_key:
        storage.delete(listing.video_key)
    listing.video_key, listing.video_type = key, content_type
    db.commit()
    return listing


def delete_video(db: Session, seller: User, listing_id: uuid.UUID) -> Listing:
    listing = _get_own(db, seller, listing_id)
    if listing.video_key:
        registry.get_storage().delete(listing.video_key)
    listing.video_key = listing.video_type = None
    db.commit()
    return listing


def video_url(listing: Listing) -> str | None:
    return registry.get_storage().url(listing.video_key) if listing.video_key else None


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
    model: str | None = None,
    condition: str | None = None,
    year_min: int | None = None,
    year_max: int | None = None,
    fits: tuple[str, str | None, int | None] | None = None,  # (make, model, year)
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
    # Photos and fitment for the whole page in two queries, not two per listing.
    stmt = stmt.options(selectinload(Listing.images), selectinload(Listing.fitments))
    # Every word must appear somewhere (title, description or SKU), in any order, so
    # "brake pads camry" finds "Brake pads - Toyota Camry". Wildcards typed by the user are
    # treated as plain characters.
    for word in (q or "").split()[:8]:
        escaped = word.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
        like = f"%{escaped}%"
        stmt = stmt.where(
            or_(Listing.title.ilike(like), Listing.description.ilike(like), Listing.sku.ilike(like))
        )
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
    if model:
        stmt = stmt.where(Listing.attributes["model"].astext.ilike(model.strip()))
    if condition:
        stmt = stmt.where(Listing.attributes["condition"].astext.ilike(condition.strip()))
    if fits:
        stmt = stmt.where(_fits_clause(*fits))
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

    if sort == "rating":
        ratings = reviews.rating_summary_subquery()
        stmt = stmt.outerjoin(ratings, ratings.c.user_id == Listing.seller_id).order_by(
            func.coalesce(ratings.c.average, 0.0).desc(),
            func.coalesce(ratings.c.count, 0).desc(),
            Listing.created_at.desc(),
            Listing.id,
        )
    elif sort == "relevance" and q:
        # pg_trgm: how well the search words match the title (then the description).
        words = q.strip()
        stmt = stmt.order_by(
            func.word_similarity(words, Listing.title).desc(),
            func.word_similarity(words, Listing.description).desc(),
            Listing.created_at.desc(),
            Listing.id,
        )
    elif sort == "price_asc":
        stmt = stmt.order_by(Listing.price_minor.asc(), Listing.id)
    elif sort == "price_desc":
        stmt = stmt.order_by(Listing.price_minor.desc(), Listing.id)
    elif sort == "distance" and distance is not None:
        stmt = stmt.order_by(distance.asc(), Listing.id)
    else:
        stmt = stmt.order_by(Listing.created_at.desc(), Listing.id)
    rows, total = paginate_page(db, stmt, page=page, page_size=page_size)
    return rows, total, counts


def _fits_clause(make: str, model: str | None, year: int | None):
    """Parts with a fitment row covering this vehicle (FR-4.2). A row with no model fits
    every model of the make; a missing year bound is open-ended."""
    fit = select(PartFitment.id).where(
        PartFitment.listing_id == Listing.id, PartFitment.make.ilike(make.strip())
    )
    if model:
        fit = fit.where(or_(PartFitment.model.is_(None), PartFitment.model.ilike(model.strip())))
    if year is not None:
        fit = fit.where(
            or_(PartFitment.year_from.is_(None), PartFitment.year_from <= year),
            or_(PartFitment.year_to.is_(None), PartFitment.year_to >= year),
        )
    return fit.exists()


def fitment_query(
    db: Session,
    viewer: User | None,
    *,
    vehicle_id: uuid.UUID | None,
    make: str | None,
    model: str | None,
    year: int | None,
) -> tuple[str, str | None, int | None] | None:
    """Turn the "fits my vehicle" search options into (make, model, year), or None.

    `vehicle_id` picks a car from the signed-in owner's garage and wins over the rest.
    """
    if vehicle_id:
        if viewer is None:
            raise Unauthorized(
                "Sign in to search by a vehicle in your garage", code="not_authenticated"
            )
        vehicle = get_vehicle(db, viewer, vehicle_id)
        return vehicle.make, vehicle.model, vehicle.year
    if make:
        return make, model, year
    if model or year is not None:
        raise BadRequest("Give fits_make with fits_model or fits_year", code="fits_make_required")
    return None


def _counts_by_type(db: Session, filtered) -> dict[str, int]:
    sub = filtered.subquery()
    rows = db.execute(select(sub.c.type, func.count()).group_by(sub.c.type)).all()
    counts = {t.value: 0 for t in ListingType}
    counts.update({str(t): n for t, n in rows})
    return counts


MECHANIC_SORTS = ("rating", "price", "distance")


def mechanic_directory(
    db: Session,
    *,
    city: str | None,
    near: tuple[float, float] | None,
    radius_km: float,
    page: int,
    page_size: int,
    service: str | None = None,
    min_price: int | None = None,
    max_price: int | None = None,
    min_rating: float | None = None,
    sort: str = "rating",
) -> tuple[list[dict], int]:
    """Verified mechanics with at least one live service matching the filters (FR-5.2).

    `service` matches a service's title or category; the price filters apply to each
    service, and `from_price_minor` is the cheapest *matching* one. Sorting happens in SQL
    so it is right across pages: best rated (default), cheapest, or nearest (needs `near`).
    Returns plain dicts: user, rating, review count, starting price, services, distance.
    """
    # Postgres cannot take min() of an untyped NULL, so the "no distance" column is a Float.
    distance = cast(null(), Float)
    services = select(Listing.seller_id, Listing.price_minor).where(
        Listing.type == ListingType.MECHANIC_SERVICE, Listing.status == ListingStatus.LIVE
    )
    if service:
        like = f"%{service.strip()}%"
        services = services.where(
            or_(Listing.title.ilike(like), Listing.attributes["category"].astext.ilike(like))
        )
    if min_price is not None:
        services = services.where(Listing.price_minor >= min_price)
    if max_price is not None:
        services = services.where(Listing.price_minor <= max_price)
    if city:
        services = services.where(Listing.city.ilike(city.strip()))
    if near:
        point = func.ST_GeogFromText(f"SRID=4326;POINT({near[1]} {near[0]})")
        distance = func.ST_Distance(Listing.location, point)
        services = services.where(Listing.location.is_not(None)).where(
            func.ST_DWithin(Listing.location, point, radius_km * 1000)
        )
    sub = services.add_columns(distance.label("distance")).subquery()
    per_mechanic = (
        select(
            sub.c.seller_id,
            func.min(sub.c.price_minor).label("from_price"),
            func.count().label("services"),
            func.min(sub.c.distance).label("distance"),
        )
        .group_by(sub.c.seller_id)
        .subquery()
    )
    ratings = reviews.rating_summary_subquery()
    rating = func.coalesce(ratings.c.average, 0.0)
    review_count = func.coalesce(ratings.c.count, 0)
    stmt = (
        select(
            User, per_mechanic.c.from_price, per_mechanic.c.services, per_mechanic.c.distance,
            rating, review_count,
        )
        .join(per_mechanic, per_mechanic.c.seller_id == User.id)
        .outerjoin(ratings, ratings.c.user_id == User.id)
        .join(UserRole, (UserRole.user_id == User.id) & (UserRole.role == Role.MECHANIC))
        .where(UserRole.active.is_(True), User.kyc_status == KycStatus.APPROVED)
    )  # fmt: skip
    if min_rating is not None:
        stmt = stmt.where(rating >= min_rating)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0

    by_rating = (rating.desc(), review_count.desc(), User.created_at, User.id)
    if sort == "price":
        stmt = stmt.order_by(per_mechanic.c.from_price.asc(), *by_rating)
    elif sort == "distance" and near:
        stmt = stmt.order_by(per_mechanic.c.distance.asc(), *by_rating)
    else:
        stmt = stmt.order_by(*by_rating)
    rows = db.execute(stmt.limit(page_size).offset((max(page, 1) - 1) * page_size)).all()
    return [
        {
            "user": u,
            "rating": round(float(avg), 1),
            "review_count": int(count),
            "from_price_minor": price,
            "services": n,
            "distance_km": round(dist / 1000, 1) if dist is not None else None,
        }
        for u, price, n, dist, avg, count in rows
    ], total


# ======================================================================================
# 5. Inquiries
# ======================================================================================
def create_inquiry(
    db: Session,
    buyer: User,
    listing_id: uuid.UUID,
    message: str,
    offer_minor: int | None,
    *,
    kind: InquiryKind | None = None,
    viewing_at: datetime | None = None,
) -> ListingInquiry:
    """A question, a price offer, or a request to inspect / test-drive a vehicle (FR-3.4)."""
    listing = get_listing(db, listing_id, buyer)
    if listing.seller_id == buyer.id:
        raise UnprocessableEntity("You cannot message your own listing", code="own_listing")
    if listing.status != ListingStatus.LIVE:
        raise UnprocessableEntity("This listing is not accepting enquiries", code="not_live")
    kind = kind or (InquiryKind.OFFER if offer_minor is not None else InquiryKind.QUESTION)
    if kind == InquiryKind.OFFER and offer_minor is None:
        raise BadRequest("Give the amount you offer (offer_minor)", code="offer_required")
    if kind in VIEWING_KINDS:
        if listing.type != ListingType.VEHICLE:
            raise UnprocessableEntity(
                "Inspections and test drives are for vehicles", code="vehicles_only"
            )
        _check_viewing_time(viewing_at)
    elif viewing_at is not None:
        raise BadRequest(
            "A time is only for inspections and test drives", code="viewing_time_not_allowed"
        )
    inquiry = ListingInquiry(
        listing_id=listing.id, buyer_id=buyer.id, message=message.strip(), offer_minor=offer_minor,
        kind=kind, viewing_at=viewing_at,
    )  # fmt: skip
    db.add(inquiry)
    db.commit()
    publish(
        "inquiry.created", inquiry_id=str(inquiry.id), listing_id=str(listing.id), kind=kind.value
    )
    return inquiry


def _check_viewing_time(viewing_at: datetime | None) -> None:
    if viewing_at is None or viewing_at.tzinfo is None or viewing_at <= utcnow():
        raise BadRequest(
            "Pick a future time with a timezone offset, e.g. 2026-12-01T10:00:00+01:00",
            code="invalid_time",
        )


def my_inquiries(db: Session, buyer: User) -> list[ListingInquiry]:
    """The buyer's own questions, offers and viewing requests, newest first."""
    return list(
        db.scalars(
            select(ListingInquiry)
            .where(ListingInquiry.buyer_id == buyer.id)
            .order_by(ListingInquiry.created_at.desc())
            .limit(200)
        )
    )


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
    db: Session,
    seller: User,
    inquiry_id: uuid.UUID,
    accept: bool,
    viewing_at: datetime | None = None,
) -> ListingInquiry:
    """Accept or decline. Accepting a viewing may move it to `viewing_at` instead."""
    inquiry = db.get(ListingInquiry, inquiry_id)
    listing = db.get(Listing, inquiry.listing_id) if inquiry else None
    if inquiry is None or listing is None or listing.seller_id != seller.id:
        raise NotFound("Enquiry not found", code="inquiry_not_found")
    if inquiry.status != InquiryStatus.OPEN:
        raise UnprocessableEntity("This enquiry was already answered", code="already_answered")
    if viewing_at is not None:
        if inquiry.kind not in VIEWING_KINDS or not accept:
            raise BadRequest(
                "A new time can only be given when accepting a viewing",
                code="viewing_time_not_allowed",
            )
        _check_viewing_time(viewing_at)
        inquiry.viewing_at = viewing_at
    inquiry.status = InquiryStatus.ACCEPTED if accept else InquiryStatus.DECLINED
    db.commit()
    publish(
        "inquiry.answered",
        inquiry_id=str(inquiry.id),
        buyer_id=str(inquiry.buyer_id),
        accepted=accept,
        kind=inquiry.kind.value,
        viewing_at=inquiry.viewing_at.isoformat() if inquiry.viewing_at else None,
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


# ======================================================================================
# 7. Recommendations (FR-8.4)
# ======================================================================================
def recommendations(db: Session, user: User, *, limit: int = 20) -> list[tuple[str, Listing]]:
    """Simple, explainable suggestions, each with the reason shown to the person:

    * parts that fit the vehicles in their garage,
    * more parts from sellers they have bought from,
    * top-rated mechanic services in their city,
    * insurance products, when they have a vehicle.
    """
    out: list[tuple[str, Listing]] = []
    seen: set[uuid.UUID] = set()

    def add(reason: str, rows) -> None:
        for row in rows:
            if row.id not in seen and row.seller_id != user.id:
                seen.add(row.id)
                out.append((reason, row))

    live = select(Listing).where(Listing.status == ListingStatus.LIVE)
    newest = (Listing.created_at.desc(), Listing.id)
    vehicles = list_vehicles(db, user)[:5]
    for v in vehicles:
        parts = live.where(Listing.type == ListingType.PART, _fits_clause(v.make, v.model, v.year))
        add(f"Fits your {v.year} {v.make} {v.model}", db.scalars(parts.order_by(*newest).limit(4)))

    bought_from = select(Transaction.provider_id).where(
        Transaction.customer_id == user.id,
        Transaction.kind == TransactionKind.PARTS_ORDER,
        Transaction.paid_at.is_not(None),
    )
    add(
        "From a seller you have bought from",
        db.scalars(
            live.where(Listing.type == ListingType.PART, Listing.seller_id.in_(bought_from))
            .order_by(*newest)
            .limit(4)
        ),
    )

    if user.city:
        ratings = reviews.rating_summary_subquery()
        services = (
            live.where(
                Listing.type == ListingType.MECHANIC_SERVICE, Listing.city.ilike(user.city.strip())
            )
            .outerjoin(ratings, ratings.c.user_id == Listing.seller_id)
            .order_by(func.coalesce(ratings.c.average, 0.0).desc(), *newest)
            .limit(4)
        )
        add(f"Top-rated mechanic services in {user.city}", db.scalars(services))

    if vehicles:
        add(
            "Insurance for your vehicles",
            db.scalars(
                live.where(Listing.type == ListingType.INSURANCE_PRODUCT).order_by(*newest).limit(3)
            ),
        )
    return out[:limit]


# ======================================================================================
# 8. Part requests (FR-4.6)
# ======================================================================================
def create_part_request(
    db: Session,
    buyer: User,
    *,
    description: str,
    vehicle_id: uuid.UUID | None,
    make: str | None,
    model: str | None,
    year: int | None,
    city: str | None,
) -> PartRequest:
    """Ask parts sellers for a part that is not listed (or to confirm one fits)."""
    if vehicle_id:
        vehicle = get_vehicle(db, buyer, vehicle_id)
        make, model, year = vehicle.make, vehicle.model, vehicle.year
    if not (make or "").strip():
        raise BadRequest("Say which vehicle the part is for", code="vehicle_required")
    request = PartRequest(
        buyer_id=buyer.id, vehicle_id=vehicle_id, make=make.strip(),
        model=(model or "").strip() or None, year=year, description=description.strip(),
        city=(city or buyer.city or "").strip() or None,
    )  # fmt: skip
    db.add(request)
    db.commit()
    publish(
        "part_request.created", request_id=str(request.id), buyer_id=str(buyer.id),
        make=request.make, model=request.model or "", ref=request.ref,
    )  # fmt: skip
    return request


def _get_part_request(db: Session, request_id: uuid.UUID) -> PartRequest:
    request = db.get(PartRequest, request_id)
    if request is None:
        raise NotFound("Part request not found", code="part_request_not_found")
    return request


def can_see_part_request(request: PartRequest, viewer: User) -> bool:
    return (
        viewer.id == request.buyer_id
        or viewer.has_role(*ADMIN_ROLES)
        or (request.status == PartRequestStatus.OPEN and viewer.has_role(Role.PARTS_PROVIDER))
    )


def get_part_request(db: Session, request_id: uuid.UUID, viewer: User) -> PartRequest:
    request = _get_part_request(db, request_id)
    if not can_see_part_request(request, viewer):
        raise NotFound("Part request not found", code="part_request_not_found")
    return request


def visible_quotes(request: PartRequest, viewer: User) -> list[PartQuote]:
    """The buyer and staff see every quote (cheapest first); a seller sees only theirs."""
    if viewer.id == request.buyer_id or viewer.has_role(*ADMIN_ROLES):
        return sorted(request.quotes, key=lambda q: (q.price_minor, q.created_at))
    return [q for q in request.quotes if q.provider_id == viewer.id]


def open_part_requests(
    db: Session,
    provider: User,
    *,
    make: str | None,
    city: str | None,
    page: int,
    page_size: int,
) -> tuple[list[PartRequest], int]:
    """The parts sellers' feed: open requests from other people, newest first."""
    stmt = select(PartRequest).where(
        PartRequest.status == PartRequestStatus.OPEN, PartRequest.buyer_id != provider.id
    )
    if make:
        stmt = stmt.where(PartRequest.make.ilike(make.strip()))
    if city:
        stmt = stmt.where(PartRequest.city.ilike(city.strip()))
    return paginate_page(
        db, stmt.order_by(PartRequest.created_at.desc()), page=page, page_size=page_size
    )


def my_part_requests(db: Session, buyer: User) -> list[PartRequest]:
    return list(
        db.scalars(
            select(PartRequest)
            .where(PartRequest.buyer_id == buyer.id)
            .order_by(PartRequest.created_at.desc())
            .limit(200)
        )
    )


def quote_part_request(
    db: Session,
    provider: User,
    request_id: uuid.UUID,
    *,
    price_minor: int,
    message: str,
    listing_id: uuid.UUID | None,
) -> PartQuote:
    """Send (or revise) a quote. A linked listing must be one of the seller's live parts."""
    request = _get_part_request(db, request_id)
    if request.status != PartRequestStatus.OPEN:
        raise UnprocessableEntity("This request is closed", code="request_closed")
    if request.buyer_id == provider.id:
        raise UnprocessableEntity("You cannot quote on your own request", code="own_request")
    if listing_id:
        listing = db.get(Listing, listing_id)
        if (
            listing is None or listing.seller_id != provider.id
            or listing.type != ListingType.PART or listing.status != ListingStatus.LIVE
        ):  # fmt: skip
            raise NotFound("Link one of your live parts listings", code="listing_not_found")
    quote = next((q for q in request.quotes if q.provider_id == provider.id), None)
    if quote is None:
        quote = PartQuote(provider_id=provider.id)
        request.quotes.append(quote)
    quote.price_minor, quote.message, quote.listing_id = price_minor, message.strip(), listing_id
    db.commit()
    publish(
        "part_request.quoted", request_id=str(request.id), buyer_id=str(request.buyer_id),
        ref=request.ref,
    )  # fmt: skip
    return quote


def close_part_request(db: Session, buyer: User, request_id: uuid.UUID) -> PartRequest:
    request = _get_part_request(db, request_id)
    if request.buyer_id != buyer.id:
        raise NotFound("Part request not found", code="part_request_not_found")
    if request.status == PartRequestStatus.OPEN:
        request.status, request.closed_at = PartRequestStatus.CLOSED, utcnow()
        db.commit()
    return request


def sellers_stocking_make(db: Session, make: str, *, limit: int = 50) -> list[uuid.UUID]:
    """Parts sellers with a live part that fits `make`: who to tell about a part request."""
    return list(
        db.scalars(
            select(Listing.seller_id)
            .join(PartFitment, PartFitment.listing_id == Listing.id)
            .where(
                Listing.type == ListingType.PART,
                Listing.status == ListingStatus.LIVE,
                PartFitment.make.ilike(make),
            )
            .distinct()
            .limit(limit)
        )
    )


@subscribe("user.removed")
def _take_down_removed_users_listings(event: Event) -> None:
    """An admin removed the account (FR-13.2): its pending and live listings come down."""
    db = db_module.new_session()  # looked up at call time so tests can swap it
    try:
        db.execute(
            update(Listing)
            .where(
                Listing.seller_id == uuid.UUID(event.payload["user_id"]),
                Listing.status.in_([ListingStatus.PENDING, ListingStatus.LIVE]),
            )
            .values(status=ListingStatus.REMOVED)
        )
        db.commit()
    finally:
        db.close()
