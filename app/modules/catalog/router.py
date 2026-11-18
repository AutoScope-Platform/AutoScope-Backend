"""Catalog endpoints: garage, listings, search, mechanics, enquiries and admin review."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import error_responses
from app.core.legal import insurance_disclosure
from app.core.permissions import ADMIN_ROLES
from app.core.ratelimit import client_ip
from app.modules.catalog import service
from app.modules.catalog.models import Listing, ListingStatus, ListingType
from app.modules.catalog.schemas import (
    DecideIn,
    FitmentOut,
    ImageOut,
    InquiryAnswerIn,
    InquiryIn,
    InquiryOut,
    ListingIn,
    ListingOut,
    ListingPage,
    ListingStatsOut,
    ListingUpdateIn,
    MechanicOut,
    MechanicPage,
    RecommendationOut,
    RecommendationsOut,
    ReviewQueueOut,
    SellerOut,
    ServiceRecordIn,
    ServiceRecordOut,
    StatusIn,
    SuspendIn,
    VehicleIn,
    VehicleOut,
    VehicleUpdateIn,
    VinOut,
)
from app.modules.identity.deps import (
    CurrentUser,
    OptionalUser,
    require_admin,
    user_rate_limit,
)
from app.modules.identity.models import KycStatus, User
from app.modules.reviews import service as reviews

router = APIRouter(tags=["Catalog"])


# --- Output builders (listings need their seller and photo links attached) -------------------
def _listings_out(db: Session, rows: list[Listing], viewer: User | None) -> list[ListingOut]:
    seller_ids = list({r.seller_id for r in rows})
    sellers = (
        {u.id: u for u in db.query(User).filter(User.id.in_(seller_ids))} if seller_ids else {}
    )
    ratings = reviews.ratings_for(db, seller_ids)
    staff = bool(viewer and viewer.has_role(*ADMIN_ROLES))
    mine = [r for r in rows if staff or (viewer is not None and viewer.id == r.seller_id)]
    stats = service.listing_stats(db, mine)
    out = []
    for r in rows:
        seller = sellers[r.seller_id]
        avg, count = ratings.get(seller.id, (0.0, 0))
        private = staff or (viewer is not None and viewer.id == r.seller_id)
        out.append(
            ListingOut(
                id=r.id, ref=r.ref, type=r.type, status=r.status, title=r.title,
                description=r.description, price_minor=r.price_minor, currency=r.currency,
                city=r.city, latitude=r.latitude, longitude=r.longitude, stock=r.stock,
                sku=r.sku,
                fits=[FitmentOut(make=f.make, model=f.model, year_from=f.year_from, year_to=f.year_to) for f in r.fitments],
                vin=r.vin, attributes=r.attributes, created_at=r.created_at,
                images=[ImageOut(id=i.id, url=service.image_url(i), position=i.position) for i in r.images],
                seller=SellerOut(
                    id=seller.id, name=seller.display_name, city=seller.city,
                    verified=seller.kyc_status == KycStatus.APPROVED,
                    rating=avg, review_count=count,
                ),
                disclosure=insurance_disclosure(seller.display_name) if r.type == ListingType.INSURANCE_PRODUCT else None,
                video_url=service.video_url(r),
                stats=ListingStatsOut(**stats[r.id]) if r.id in stats else None,
                rejection_reason=r.rejection_reason if private else None,
                fraud_signals=list(r.fraud_signals or []) if private else [],
            )
        )  # fmt: skip
    return out


def _one(db: Session, listing: Listing, viewer: User | None) -> ListingOut:
    return _listings_out(db, [listing], viewer)[0]


# --- Garage ------------------------------------------------------------------------------
@router.get("/me/vehicles", response_model=list[VehicleOut], summary="My garage")
def my_vehicles(user: CurrentUser, db: Session = Depends(get_db)):
    return service.list_vehicles(db, user)


@router.post(
    "/me/vehicles",
    response_model=VehicleOut,
    status_code=status.HTTP_201_CREATED,
    summary="Add a vehicle to my garage",
    responses=error_responses(400, 409),
)
def add_vehicle(body: VehicleIn, user: CurrentUser, db: Session = Depends(get_db)):
    return service.add_vehicle(db, user, body.model_dump())


@router.get(
    "/vehicles/vin/{vin}",
    response_model=VinOut,
    summary="Decode a VIN into make, model and year",
    description="Use it to pre-fill the 'add vehicle' form.",
    responses=error_responses(400, 404, 422),
)
def decode_vin(vin: str, user: CurrentUser) -> VinOut:
    info = service.decode_vin(vin)
    return VinOut(**info.__dict__)


@router.patch(
    "/me/vehicles/{vehicle_id}",
    response_model=VehicleOut,
    summary="Edit a garage vehicle",
    responses=error_responses(404),
)
def update_vehicle(
    vehicle_id: uuid.UUID, body: VehicleUpdateIn, user: CurrentUser, db: Session = Depends(get_db)
):
    return service.update_vehicle(db, user, vehicle_id, body.model_dump(exclude_unset=True))


@router.delete(
    "/me/vehicles/{vehicle_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a vehicle from my garage",
    responses=error_responses(404),
)
def delete_vehicle(vehicle_id: uuid.UUID, user: CurrentUser, db: Session = Depends(get_db)) -> None:
    service.delete_vehicle(db, user, vehicle_id)


@router.get(
    "/me/vehicles/{vehicle_id}/service-records",
    response_model=list[ServiceRecordOut],
    summary="A vehicle's service history",
    responses=error_responses(404),
)
def service_records(vehicle_id: uuid.UUID, user: CurrentUser, db: Session = Depends(get_db)):
    return service.get_vehicle(db, user, vehicle_id).service_records


@router.post(
    "/me/vehicles/{vehicle_id}/service-records",
    response_model=ServiceRecordOut,
    status_code=status.HTTP_201_CREATED,
    summary="Add a service entry by hand",
    responses=error_responses(400, 404),
)
def add_service_record(
    vehicle_id: uuid.UUID, body: ServiceRecordIn, user: CurrentUser, db: Session = Depends(get_db)
):
    vehicle = service.get_vehicle(db, user, vehicle_id)
    return service.add_service_record(db, vehicle, body.model_dump())


# --- Search ------------------------------------------------------------------------------
def _near(lat: float | None, lng: float | None) -> tuple[float, float] | None:
    return (lat, lng) if lat is not None and lng is not None else None


@router.get(
    "/listings",
    response_model=ListingPage,
    summary="Search listings (vehicles, parts, services, insurance)",
    description="Public. Pass `lat` and `lng` to filter by distance (`radius_km`) with "
    "`sort=distance`. `counts_by_type` ignores the `type` filter so apps can show tab counts.\n\n"
    "Vehicles: `make`, `model`, `year_min`/`year_max`, `condition` (FR-3.2). Parts that fit a "
    "vehicle (FR-4.2): `vehicle_id` (a car in your garage, signed in) or `fits_make` with "
    "optional `fits_model` and `fits_year`. `q` also matches a part's SKU.",
    responses=error_responses(400, 401, 404),
)
def search(
    viewer: OptionalUser,
    q: str | None = None,
    type: ListingType | None = None,
    city: str | None = None,
    min_price: int | None = Query(None, ge=0),
    max_price: int | None = Query(None, ge=0),
    make: str | None = None,
    model: str | None = None,
    condition: str | None = Query(None, examples=["used"]),
    year_min: int | None = None,
    year_max: int | None = None,
    vehicle_id: uuid.UUID | None = Query(None, description="Parts that fit this garage vehicle."),
    fits_make: str | None = None,
    fits_model: str | None = None,
    fits_year: int | None = Query(None, ge=1950, le=2100),
    seller_id: uuid.UUID | None = None,
    lat: float | None = Query(None, ge=-90, le=90),
    lng: float | None = Query(None, ge=-180, le=180),
    radius_km: float = Query(25, gt=0, le=2000),
    sort: str = Query(
        "newest",
        pattern="^(newest|price_asc|price_desc|distance|rating|relevance)$",
        description="`rating`: best-rated sellers first. `relevance`: best match for `q` "
        "first (needs `q`). `distance`: needs `lat` and `lng`.",
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> ListingPage:
    fits = service.fitment_query(
        db, viewer, vehicle_id=vehicle_id, make=fits_make, model=fits_model, year=fits_year
    )
    rows, total, counts = service.search_listings(
        db, q=q, type_=type, city=city, min_price=min_price, max_price=max_price, make=make,
        model=model, condition=condition, year_min=year_min, year_max=year_max, fits=fits,
        seller_id=seller_id, near=_near(lat, lng), radius_km=radius_km, sort=sort, page=page,
        page_size=page_size,
    )  # fmt: skip
    return ListingPage(
        items=_listings_out(db, rows, viewer), total=total, page=page, page_size=page_size,
        counts_by_type=counts,
    )  # fmt: skip


@router.get(
    "/mechanics",
    response_model=MechanicPage,
    summary="Find verified mechanics",
    description="Mechanics with at least one live service matching the filters (FR-5.2). "
    "`service` matches a service's title or category (e.g. `brakes`); `min_price`/`max_price` "
    "apply to each service, in kobo. Sort by `rating` (default), `price` (cheapest matching "
    "service) or `distance` (needs `lat` and `lng`).",
)
def mechanics(
    service_q: str | None = Query(None, alias="service", examples=["brakes"]),
    min_price: int | None = Query(None, ge=0),
    max_price: int | None = Query(None, ge=0),
    min_rating: float | None = Query(None, ge=0, le=5),
    city: str | None = None,
    lat: float | None = Query(None, ge=-90, le=90),
    lng: float | None = Query(None, ge=-180, le=180),
    radius_km: float = Query(25, gt=0, le=2000),
    sort: str = Query("rating", pattern="^(rating|price|distance)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> MechanicPage:
    items, total = service.mechanic_directory(
        db, city=city, near=_near(lat, lng), radius_km=radius_km, page=page, page_size=page_size,
        service=service_q, min_price=min_price, max_price=max_price, min_rating=min_rating,
        sort=sort,
    )  # fmt: skip
    return MechanicPage(
        items=[
            MechanicOut(
                id=i["user"].id, name=i["user"].display_name, city=i["user"].city,
                rating=i["rating"], review_count=i["review_count"],
                from_price_minor=i["from_price_minor"], services=i["services"],
                distance_km=i["distance_km"],
            )
            for i in items
        ],
        total=total,
    )  # fmt: skip


# --- Listings: seller actions -----------------------------------------------------------
@router.post(
    "/listings",
    response_model=ListingOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a listing (goes to admin review)",
    responses=error_responses(400, 403, 422, 429),
    dependencies=[Depends(user_rate_limit("listing-create", 10, 60))],
)
def create_listing(body: ListingIn, user: CurrentUser, db: Session = Depends(get_db)):
    listing = service.create_listing(db, user, body.model_dump())
    return _one(db, listing, user)


@router.get("/me/listings", response_model=ListingPage, summary="My listings")
def my_listings(
    user: CurrentUser,
    status: ListingStatus | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> ListingPage:
    rows, total = service.my_listings(db, user, status, page, page_size)
    return ListingPage(
        items=_listings_out(db, rows, user), total=total, page=page, page_size=page_size
    )


@router.get(
    "/listings/{listing_id}",
    response_model=ListingOut,
    summary="One listing",
    responses=error_responses(404),
)
def get_listing(listing_id: uuid.UUID, viewer: OptionalUser, db: Session = Depends(get_db)):
    """Opening a listing counts a view for the seller's stats (not the seller's own views)."""
    return _one(db, service.view_listing(db, listing_id, viewer), viewer)


@router.patch(
    "/listings/{listing_id}",
    response_model=ListingOut,
    summary="Edit my listing",
    description="Editing a rejected listing resubmits it. A live listing that picks up a new "
    "fraud signal goes back to review.",
    responses=error_responses(404, 422),
)
def update_listing(
    listing_id: uuid.UUID, body: ListingUpdateIn, user: CurrentUser, db: Session = Depends(get_db)
):
    listing = service.update_listing(db, user, listing_id, body.model_dump(exclude_unset=True))
    return _one(db, listing, user)


@router.put(
    "/listings/{listing_id}/status",
    response_model=ListingOut,
    summary="Mark live, reserved or sold",
    responses=error_responses(404, 422),
)
def set_status(
    listing_id: uuid.UUID, body: StatusIn, user: CurrentUser, db: Session = Depends(get_db)
):
    return _one(db, service.set_seller_status(db, user, listing_id, body.status), user)


@router.delete(
    "/listings/{listing_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete my listing",
    responses=error_responses(404),
)
def remove_listing(listing_id: uuid.UUID, user: CurrentUser, db: Session = Depends(get_db)) -> None:
    service.remove_listing(db, user, listing_id)


@router.post(
    "/listings/{listing_id}/images",
    response_model=ImageOut,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a photo (JPEG, PNG or WebP, up to 8 MB)",
    responses=error_responses(400, 404, 422, 429),
    dependencies=[Depends(user_rate_limit("listing-image", 40, 60))],
)
async def upload_image(
    listing_id: uuid.UUID,
    user: CurrentUser,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> ImageOut:
    image = service.add_image(db, user, listing_id, file.content_type or "", await file.read())
    return ImageOut(id=image.id, url=service.image_url(image), position=image.position)


@router.delete(
    "/listings/{listing_id}/images/{image_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a photo",
    responses=error_responses(404),
)
def delete_image(
    listing_id: uuid.UUID, image_id: uuid.UUID, user: CurrentUser, db: Session = Depends(get_db)
) -> None:
    service.delete_image(db, user, listing_id, image_id)


# --- Enquiries and offers ----------------------------------------------------------------
@router.post(
    "/listings/{listing_id}/inquiries",
    response_model=InquiryOut,
    status_code=status.HTTP_201_CREATED,
    summary="Ask a question or make an offer",
    responses=error_responses(404, 422),
)
def create_inquiry(
    listing_id: uuid.UUID, body: InquiryIn, user: CurrentUser, db: Session = Depends(get_db)
):
    return service.create_inquiry(
        db, user, listing_id, body.message, body.offer_minor, kind=body.kind,
        viewing_at=body.viewing_at,
    )  # fmt: skip


@router.get(
    "/listings/{listing_id}/inquiries",
    response_model=list[InquiryOut],
    summary="Enquiries on my listing",
    responses=error_responses(404),
)
def list_inquiries(listing_id: uuid.UUID, user: CurrentUser, db: Session = Depends(get_db)):
    return service.list_inquiries(db, user, listing_id)


@router.post(
    "/inquiries/{inquiry_id}/answer",
    response_model=InquiryOut,
    summary="Accept or decline an enquiry or offer",
    responses=error_responses(404, 422),
)
def answer_inquiry(
    inquiry_id: uuid.UUID, body: InquiryAnswerIn, user: CurrentUser, db: Session = Depends(get_db)
):
    return service.respond_to_inquiry(db, user, inquiry_id, body.accept, body.viewing_at)


# --- Admin: listing review ---------------------------------------------------------------
@router.get(
    "/admin/listings",
    response_model=ReviewQueueOut,
    summary="Listing review queue (admin)",
    description="Pending listings by default, oldest first; `flagged=true` shows only those "
    "with fraud signals.",
)
def admin_queue(
    admin: User = Depends(require_admin),
    status: ListingStatus | None = None,
    flagged: bool = False,
    page: int = Query(1, ge=1),
    page_size: int = Query(15, ge=1, le=100),
    db: Session = Depends(get_db),
) -> ReviewQueueOut:
    rows, total = service.review_queue(
        db, status=status, flagged=flagged, page=page, page_size=page_size
    )
    return ReviewQueueOut(
        items=_listings_out(db, rows, admin), total=total, counts=service.review_counts(db)
    )


@router.post(
    "/admin/listings/{listing_id}/decision",
    response_model=ListingOut,
    summary="Approve or reject a pending listing",
    responses=error_responses(400, 404, 422),
)
def admin_decide(
    listing_id: uuid.UUID,
    body: DecideIn,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    listing = service.decide_listing(
        db, listing_id, approve=body.approve, reason=body.reason, admin=admin, ip=client_ip(request)
    )
    return _one(db, listing, admin)


@router.post(
    "/admin/listings/{listing_id}/suspend",
    response_model=ListingOut,
    summary="Take a listing down",
    responses=error_responses(404),
)
def admin_suspend(
    listing_id: uuid.UUID,
    body: SuspendIn,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    listing = service.suspend_listing(
        db, listing_id, reason=body.reason, admin=admin, ip=client_ip(request)
    )
    return _one(db, listing, admin)


# --- Video, my enquiries, recommendations ------------------------------------------------
@router.post(
    "/listings/{listing_id}/video",
    response_model=ListingOut,
    summary="Upload the listing's video (MP4, MOV or WebM, up to 50 MB)",
    description="One video per listing (FR-3.1); uploading again replaces it.",
    responses=error_responses(400, 404, 429),
    dependencies=[Depends(user_rate_limit("listing-video", 10, 60))],
)
async def upload_video(
    listing_id: uuid.UUID,
    user: CurrentUser,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> ListingOut:
    data = await file.read(service.VIDEO_MAX_BYTES + 1)
    return _one(db, service.set_video(db, user, listing_id, data), user)


@router.delete(
    "/listings/{listing_id}/video",
    response_model=ListingOut,
    summary="Remove the listing's video",
    responses=error_responses(404),
)
def delete_video(listing_id: uuid.UUID, user: CurrentUser, db: Session = Depends(get_db)):
    return _one(db, service.delete_video(db, user, listing_id), user)


@router.get(
    "/me/inquiries",
    response_model=list[InquiryOut],
    summary="My questions, offers, inspections and test drives (as a buyer)",
)
def my_inquiries(user: CurrentUser, db: Session = Depends(get_db)):
    return service.my_inquiries(db, user)


@router.get(
    "/me/recommendations",
    response_model=RecommendationsOut,
    summary="Suggestions for me (FR-8.4)",
    description="Parts that fit my garage vehicles, more from sellers I bought from, "
    "top-rated mechanic services in my city and insurance for my vehicles. Each item says why.",
)
def my_recommendations(user: CurrentUser, db: Session = Depends(get_db)) -> RecommendationsOut:
    pairs = service.recommendations(db, user)
    listings = _listings_out(db, [listing for _, listing in pairs], user)
    return RecommendationsOut(
        items=[
            RecommendationOut(reason=reason, listing=out)
            for (reason, _), out in zip(pairs, listings, strict=True)
        ]
    )
