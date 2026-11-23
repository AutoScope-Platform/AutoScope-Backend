"""Demo data for local development and the staging demo.   Run: `make seed`

Creates staff, providers, customers, listings and a handful of paid/completed/disputed
transactions that mirror the admin dashboard mock. It goes through the real service
functions where money is involved, so ledger rows, escrow and notifications are genuine.

Safe to run twice: it stops if the demo admin already exists. It refuses to run in
production because every demo account shares a published password.

Demo logins (password for everyone: AutoScope#2026; SMS/e-mail code with mock drivers: 123456):
    adaora@autoscope.ng   Super admin        support@autoscope.ng   Support
    ola.motors@demo.ng    Mechanic           lekki.autos@demo.ng    Dealer
    kunle@demo.ng         Vehicle owner      fastlane@demo.ng       Haulage provider
"""

from __future__ import annotations

import logging

from geoalchemy2.elements import WKTElement
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import db as db_module
from app.core.config import get_settings
from app.core.db import utcnow
from app.core.permissions import BUSINESS_ROLES, Role
from app.core.security import hash_password
from app.modules.catalog.models import Listing, ListingStatus, ListingType, PartFitment
from app.modules.identity.models import KycStatus, User, UserRole
from app.modules.payments import service as payments
from app.modules.payments.models import PaymentMethod, TransactionKind

log = logging.getLogger("seed")
PASSWORD = "AutoScope#2026"

# Lagos neighbourhoods (lat, lng) so distance search and haulage dispatch have real geometry.
LAGOS = {
    "Lekki": (6.4474, 3.4700),
    "Ikeja": (6.6018, 3.3515),
    "Yaba": (6.5095, 3.3711),
    "Surulere": (6.4969, 3.3548),
    "Victoria Island": (6.4281, 3.4219),
    "Ikorodu": (6.6194, 3.5105),
}


def _user(db: Session, name: str, email: str, *roles: Role, business: str | None = None,
          city: str = "Lagos", phone: str | None = None, two_factor: bool = False) -> User:  # fmt: skip
    """Create one verified account. Business roles are pre-approved so their listings go live."""
    is_business = any(r in BUSINESS_ROLES for r in roles)
    user = User(
        full_name=name, business_name=business, email=email, phone=phone, city=city,
        password_hash=hash_password(PASSWORD), verified_at=utcnow(), two_factor_enabled=two_factor,
        kyc_status=KycStatus.APPROVED if is_business else KycStatus.NOT_REQUIRED,
    )  # fmt: skip
    user.roles = [UserRole(role=r) for r in roles]
    db.add(user)
    db.flush()
    return user


def _listing(db: Session, seller: User, type_: ListingType, title: str, price_naira: int, area: str,
             status: ListingStatus = ListingStatus.LIVE, stock: int | None = None,
             attributes: dict | None = None, vin: str | None = None, fraud: list[str] | None = None) -> Listing:  # fmt: skip
    lat, lng = LAGOS[area]
    listing = Listing(
        seller_id=seller.id, type=type_, status=status, title=title,
        description=f"{title} - demo listing.", price_minor=price_naira * 100, city="Lagos",
        latitude=lat, longitude=lng, location=WKTElement(f"POINT({lng} {lat})", srid=4326),
        attributes=attributes or {}, stock=stock, vin=vin, fraud_signals=fraud or [],
    )  # fmt: skip
    db.add(listing)
    db.flush()
    return listing


def _pay(db: Session, customer: User, tx) -> None:
    """Take a payment through the mock gateway exactly as the app would."""
    payments.request_payment(db, tx)
    reference = payments.start_payment(db, customer, tx.id, PaymentMethod.CARD)[0].reference
    payments.confirm_payment(db, reference)


def run(db: Session) -> None:
    if db.scalar(select(User).where(User.email == "adaora@autoscope.ng")):
        log.info("Demo data already present - nothing to do.")
        return

    # --- Staff ------------------------------------------------------------------------
    _user(
        db, "Adaora Eze", "adaora@autoscope.ng", Role.ADMIN, phone="+2348010000001", two_factor=True
    )
    _user(
        db,
        "Tunde Bello",
        "support@autoscope.ng",
        Role.SUPPORT,
        phone="+2348010000002",
        two_factor=True,
    )

    # --- Customers and providers ---------------------------------------------------------
    kunle = _user(db, "Kunle Adeyemi", "kunle@demo.ng", Role.VEHICLE_OWNER, phone="+2348020000001")
    chioma = _user(
        db, "Chioma Okafor", "chioma@demo.ng", Role.VEHICLE_OWNER, phone="+2348020000002"
    )
    dealer = _user(db, "Femi Johnson", "lekki.autos@demo.ng", Role.DEALER, business="Lekki Autos")
    parts = _user(
        db, "Ngozi Parts", "ngozi.parts@demo.ng", Role.PARTS_PROVIDER, business="Ngozi Auto Parts"
    )
    mech = _user(db, "Ola Balogun", "ola.motors@demo.ng", Role.MECHANIC, business="Ola Motors")
    _user(db, "Musa Ibrahim", "fastlane@demo.ng", Role.HAULAGE_PROVIDER, business="FastLane Towing")
    insurer = _user(
        db, "Sade Williams", "cover@demo.ng", Role.INSURER, business="SafeRoad Insurance"
    )

    # --- Listings --------------------------------------------------------------------------
    car = _listing(db, dealer, ListingType.VEHICLE, "2018 Toyota Camry XLE", 9_800_000, "Lekki",
                   attributes={"make": "Toyota", "model": "Camry", "year": 2018, "mileage_km": 64000},
                   vin="4T1BF1FK5CU123456")  # fmt: skip
    _listing(db, dealer, ListingType.VEHICLE, "2015 Honda Accord", 6_200_000, "Victoria Island",
             attributes={"make": "Honda", "model": "Accord", "year": 2015})  # fmt: skip
    _listing(db, dealer, ListingType.VEHICLE, "2020 Lexus RX350 (unreviewed)", 24_000_000, "Ikeja",
             status=ListingStatus.PENDING, fraud=["duplicate_vin"], vin="4T1BF1FK5CU123456")  # fmt: skip
    pad = _listing(
        db, parts, ListingType.PART, "Brake pads - Toyota Camry", 28_500, "Ikeja", stock=40
    )
    # SKU and vehicle fitment so "parts that fit my car" search has something to find.
    pad.sku = "NAP-BP-CAMRY"
    pad.fitments = [
        PartFitment(make="Toyota", model="Camry", year_from=2012, year_to=2019),
        PartFitment(make="Toyota", model="Venza", year_from=2013, year_to=2016),
    ]
    air = _listing(
        db, parts, ListingType.PART, "Air filter - Honda Accord", 9_000, "Ikeja", stock=75
    )
    air.sku = "NAP-AF-ACCORD"
    air.fitments = [PartFitment(make="Honda", model="Accord", year_from=2013, year_to=2017)]
    _listing(db, parts, ListingType.PART, "Car battery 70Ah", 85_000, "Surulere",
             status=ListingStatus.PENDING, stock=12)  # fmt: skip
    svc = _listing(db, mech, ListingType.MECHANIC_SERVICE, "Full service (oil, filters, inspection)", 45_000, "Yaba",
                   attributes={"duration_minutes": 120, "pricing": "fixed", "category": "servicing"})  # fmt: skip
    _listing(db, mech, ListingType.MECHANIC_SERVICE, "Engine diagnostics", 15_000, "Yaba",
             attributes={"duration_minutes": 60, "pricing": "fixed", "category": "diagnostics"})  # fmt: skip
    _listing(db, insurer, ListingType.INSURANCE_PRODUCT, "Comprehensive cover", 120_000, "Victoria Island",
             attributes={"cover": "comprehensive"})  # fmt: skip
    _listing(db, insurer, ListingType.INSURANCE_PRODUCT, "Third-party cover", 25_000, "Victoria Island",
             attributes={"cover": "third_party"})  # fmt: skip

    # --- Money: a paid vehicle sale, a paid + completed part and booking, and a dispute ---------
    sale = payments.create_transaction(db, kind=TransactionKind.VEHICLE_SALE, customer_id=kunle.id,
                                       provider_id=dealer.id, amount_minor=car.price_minor, source_id=car.id)  # fmt: skip
    _pay(db, kunle, sale)

    order = payments.create_transaction(db, kind=TransactionKind.PARTS_ORDER, customer_id=chioma.id,
                                        provider_id=parts.id, amount_minor=pad.price_minor * 2, source_id=pad.id)  # fmt: skip
    _pay(db, chioma, order)
    payments.mark_completed(db, order)

    booking = payments.create_transaction(db, kind=TransactionKind.SERVICE_BOOKING, customer_id=kunle.id,
                                          provider_id=mech.id, amount_minor=svc.price_minor, source_id=svc.id)  # fmt: skip
    _pay(db, kunle, booking)
    payments.mark_completed(db, booking)

    disputed = payments.create_transaction(db, kind=TransactionKind.SERVICE_BOOKING, customer_id=chioma.id,
                                           provider_id=mech.id, amount_minor=15_000_00)  # fmt: skip
    _pay(db, chioma, disputed)
    payments.mark_completed(db, disputed)
    payments.open_dispute(db, chioma, disputed.id, "Diagnostics report did not match the fault.")

    db.commit()
    # The haulage provider has no seeded jobs: request one from Swagger to watch dispatch.
    log.info("Seeded demo data. Sign in as adaora@autoscope.ng / %s (code 123456).", PASSWORD)


def main() -> None:
    settings = get_settings()
    if settings.env == "production":
        raise SystemExit("Refusing to seed demo data in production.")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    with db_module.new_session() as db:
        run(db)


if __name__ == "__main__":
    main()
