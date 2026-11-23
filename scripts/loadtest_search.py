"""Search load test (FSD 5.1: search and filter results within 2 seconds at 100,000 listings).

    DATABASE_URL=postgresql://autoscope:autoscope@127.0.0.1:5432/autoscope_load \\
        python -m scripts.loadtest_search --listings 100000 --runs 20

1. Builds the schema in that database (it must be a throwaway database whose name contains
   "load"; production is refused) and fills it with realistic data: sellers, ratings, and
   `--listings` listings of all four types across Lagos, Abuja and Port Harcourt, with
   parts fitment.
2. Calls the real `GET /listings` and `GET /mechanics` endpoints in-process (so the time
   includes the database, the ORM and building the JSON, but not the network) for the
   searches the apps make, and prints p50 / p95 / max per search.

Run it again with `--reuse` to time searches without rebuilding the data.
"""

from __future__ import annotations

import argparse
import os
import random
import statistics
import sys
import time
import uuid

SEARCHES: list[tuple[str, str, dict]] = [
    ("browse newest (home)", "/api/v1/listings", {}),
    ("one word", "/api/v1/listings", {"q": "brake"}),
    ("three words, relevance", "/api/v1/listings", {"q": "brake pads camry", "sort": "relevance"}),
    ("vehicles: make+model+price", "/api/v1/listings",
     {"type": "vehicle", "make": "Toyota", "model": "Corolla", "min_price": 300_000_000, "max_price": 1_500_000_000}),
    ("vehicles: year range, price sort", "/api/v1/listings",
     {"type": "vehicle", "year_min": 2015, "year_max": 2020, "sort": "price_asc"}),
    ("parts that fit a car", "/api/v1/listings",
     {"fits_make": "Toyota", "fits_model": "Camry", "fits_year": 2016}),
    ("near me 10 km, by distance", "/api/v1/listings",
     {"lat": 6.5244, "lng": 3.3792, "radius_km": 10, "sort": "distance"}),
    ("best-rated sellers", "/api/v1/listings", {"type": "part", "sort": "rating"}),
    ("city + type", "/api/v1/listings", {"city": "Abuja", "type": "part"}),
    ("mechanics: service near me", "/api/v1/mechanics",
     {"service": "brake", "lat": 6.5244, "lng": 3.3792, "radius_km": 25}),
]  # fmt: skip

CITIES = {"Lagos": (6.5244, 3.3792), "Abuja": (9.0765, 7.3986), "Port Harcourt": (4.8156, 7.0498)}
MAKES = {
    "Toyota": ["Camry", "Corolla", "RAV4", "Highlander", "Venza"],
    "Honda": ["Accord", "Civic", "CR-V", "Pilot"],
    "Lexus": ["RX350", "ES350", "GX460"],
    "Hyundai": ["Elantra", "Sonata", "Santa Fe"],
    "Mercedes-Benz": ["C300", "E350", "GLK350"],
    "Ford": ["Explorer", "Edge", "Focus"],
}
PARTS = ["brake pads", "brake disc", "oil filter", "air filter", "spark plugs", "headlamp",
         "alternator", "radiator", "shock absorber", "timing belt", "wiper blades", "battery"]  # fmt: skip
SERVICES = [("Brake repair", "brakes"), ("Full service", "servicing"), ("Engine diagnostics", "diagnostics"),
            ("AC repair", "air-conditioning"), ("Wheel alignment", "tyres")]  # fmt: skip


def _guard(url: str) -> None:
    if os.environ.get("ENV") == "production":
        sys.exit("Refusing to run with ENV=production.")
    db_name = url.rsplit("/", 1)[-1].split("?")[0]
    if "load" not in db_name:
        sys.exit(f"Use a throwaway database whose name contains 'load' (got {db_name!r}).")


def build(n_listings: int, seed: int = 7) -> None:
    from sqlalchemy import insert, text

    from app import models  # noqa: F401  (registers every table)
    from app.core.db import Base, get_engine
    from app.core.permissions import Role
    from app.core.security import hash_password
    from app.modules.catalog.models import Listing, PartFitment
    from app.modules.identity.models import KycStatus, User, UserRole
    from app.modules.reviews.models import RatingSummary

    rnd = random.Random(seed)
    engine = get_engine()
    with engine.begin() as conn:
        for ext in ("postgis", "pg_trgm", "pgcrypto"):
            conn.execute(text(f"CREATE EXTENSION IF NOT EXISTS {ext}"))
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    started = time.perf_counter()
    password = hash_password("Loadtest#2026")
    roles = [Role.DEALER, Role.PARTS_PROVIDER, Role.MECHANIC, Role.INSURER]
    sellers = []
    for i in range(2_000):
        role = roles[i % len(roles)]
        sellers.append({
            "id": uuid.uuid4(), "full_name": f"Seller {i}", "business_name": f"Business {i}",
            "email": f"seller{i}@load.test", "password_hash": password,
            "city": rnd.choice(list(CITIES)), "kyc_status": KycStatus.APPROVED, "_role": role,
        })  # fmt: skip
    with engine.begin() as conn:
        conn.execute(insert(User), [{k: v for k, v in s.items() if k != "_role"} for s in sellers])
        conn.execute(
            insert(UserRole),
            [
                {"id": uuid.uuid4(), "user_id": s["id"], "role": s["_role"], "active": True}
                for s in sellers
            ],
        )
        conn.execute(insert(RatingSummary), [{"user_id": s["id"], "average": round(rnd.uniform(2.5, 5), 1), "count": rnd.randint(0, 80)} for s in sellers])  # fmt: skip

    by_role = {r: [s for s in sellers if s["_role"] == r] for r in roles}
    rows, fitments = [], []
    for _ in range(n_listings):
        city = rnd.choice(list(CITIES))
        lat0, lng0 = CITIES[city]
        lat, lng = lat0 + rnd.uniform(-0.25, 0.25), lng0 + rnd.uniform(-0.25, 0.25)
        make = rnd.choice(list(MAKES))
        model = rnd.choice(MAKES[make])
        kind = rnd.choices(
            ["vehicle", "part", "mechanic_service", "insurance_product"], [35, 50, 12, 3]
        )[0]
        lid = uuid.uuid4()
        if kind == "vehicle":
            year = rnd.randint(2005, 2024)
            seller, title = rnd.choice(by_role[Role.DEALER]), f"{year} {make} {model}"
            attrs = {"make": make, "model": model, "year": year, "mileage_km": rnd.randint(1_000, 250_000),
                     "condition": rnd.choice(["used", "new", "foreign-used"])}  # fmt: skip
            price = rnd.randint(150, 4_000) * 1_000_000
        elif kind == "part":
            part = rnd.choice(PARTS)
            seller, title = (
                rnd.choice(by_role[Role.PARTS_PROVIDER]),
                f"{part.title()} - {make} {model}",
            )
            attrs, price = (
                {"brand": rnd.choice(["Bosch", "Denso", "OEM"]), "condition": "new"},
                rnd.randint(5, 400) * 100_000,
            )
            fitments.append({"id": uuid.uuid4(), "listing_id": lid, "make": make, "model": model,
                             "year_from": rnd.randint(2005, 2015), "year_to": rnd.randint(2016, 2024)})  # fmt: skip
        elif kind == "mechanic_service":
            name, category = rnd.choice(SERVICES)
            seller, title = rnd.choice(by_role[Role.MECHANIC]), f"{name} ({city})"
            attrs, price = {"category": category}, rnd.randint(5, 150) * 100_000
        else:
            seller, title = (
                rnd.choice(by_role[Role.INSURER]),
                f"{rnd.choice(['Comprehensive', 'Third party'])} motor cover",
            )
            attrs, price = {"cover_type": "comprehensive"}, 0
        rows.append({
            "id": lid, "seller_id": seller["id"], "type": kind,
            "status": rnd.choices(["live", "sold", "pending"], [88, 8, 4])[0], "title": title,
            "description": f"{title}. Clean, tested, available in {city}.", "price_minor": price,
            "city": city, "latitude": lat, "longitude": lng, "attributes": attrs,
            "stock": rnd.randint(0, 40) if kind == "part" else None, "fraud_signals": [],
        })  # fmt: skip
        if len(rows) == 10_000:
            with engine.begin() as conn:
                conn.execute(insert(Listing), rows)
            rows = []
    with engine.begin() as conn:
        if rows:
            conn.execute(insert(Listing), rows)
        for start in range(0, len(fitments), 10_000):
            conn.execute(insert(PartFitment), fitments[start : start + 10_000])
        conn.execute(
            text(
                "UPDATE listings SET location = ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)::geography"
            )
        )
        conn.execute(text("ANALYZE"))
    print(f"Built {n_listings:,} listings, {len(fitments):,} fitments, {len(sellers):,} sellers "
          f"in {time.perf_counter() - started:.0f}s")  # fmt: skip


def measure(runs: int) -> list[tuple[str, float, float, float, int]]:
    from fastapi.testclient import TestClient

    from app.main import app

    results = []
    with TestClient(app) as client:
        for name, path, params in SEARCHES:
            client.get(path, params=params)  # warm-up (connections, plans)
            times, total = [], 0
            for _ in range(runs):
                t0 = time.perf_counter()
                r = client.get(path, params=params)
                times.append((time.perf_counter() - t0) * 1000)
                assert r.status_code == 200, (name, r.status_code, r.text[:200])
                total = r.json().get("total", 0)
            times.sort()
            p95 = times[min(len(times) - 1, int(len(times) * 0.95))]
            results.append((name, statistics.median(times), p95, times[-1], total))
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--listings", type=int, default=100_000)
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--reuse", action="store_true", help="keep the existing data")
    args = parser.parse_args()
    url = os.environ.get("DATABASE_URL", "")
    _guard(url)
    os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
    if not args.reuse:
        build(args.listings)
    print(f"\n{'search':36} {'p50 ms':>8} {'p95 ms':>8} {'max ms':>8} {'matches':>9}")
    worst = 0.0
    for name, p50, p95, mx, total in measure(args.runs):
        worst = max(worst, p95)
        print(f"{name:36} {p50:8.0f} {p95:8.0f} {mx:8.0f} {total:9,}")
    verdict = "PASS" if worst < 2000 else "FAIL"
    print(f"\nWorst p95: {worst:.0f} ms -> {verdict} (FSD 5.1 target: under 2,000 ms)")


if __name__ == "__main__":
    main()
