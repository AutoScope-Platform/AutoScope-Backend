"""Phase 2d: listing video (FR-3.1), inspection / test-drive requests (FR-3.4), view counts
and seller stats (FR-3.5), rating and relevance sorting (FR-8.3), recommendations (FR-8.4)
and comparing quotes from several insurers (FR-7.2)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.core.permissions import Role
from app.modules.booking.service import LAGOS
from app.modules.identity.models import KycStatus
from app.modules.reviews.models import RatingSummary
from tests.factories import auth_headers, make_user

API = "/api/v1"
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
MOV = b"\x00\x00\x00\x14ftypqt  " + b"\x00" * 64


@pytest.fixture
def admin(db):
    return make_user(db, Role.ADMIN)


@pytest.fixture
def dealer(db):
    return make_user(db, Role.DEALER, kyc=KycStatus.APPROVED, business_name="Lekki Autos")


def _live(client, admin, user, **body):
    r = client.post(f"{API}/listings", json=body, headers=auth_headers(user))
    assert r.status_code == 201, r.text
    d = client.post(
        f"{API}/admin/listings/{r.json()['id']}/decision",
        json={"approve": True},
        headers=auth_headers(admin),
    )
    assert d.status_code == 200, d.text
    return d.json()


def _car(client, admin, dealer, title="2018 Toyota Camry", **attrs):
    return _live(
        client, admin, dealer, type="vehicle", title=title, price_minor=9_000_000_00,
        attributes={"make": "Toyota", "model": "Camry", "year": 2018, "mileage_km": 1000, **attrs},
    )  # fmt: skip


def _future(days=3, hour=10):
    return (datetime.now(LAGOS) + timedelta(days=days)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )


# --- Video (FR-3.1) --------------------------------------------------------------------------
def test_listing_video(client, db, admin, dealer):
    car = _car(client, admin, dealer)
    h = auth_headers(dealer)

    def up(data):
        return client.post(
            f"{API}/listings/{car['id']}/video", files={"file": ("v", data, "video/mp4")}, headers=h
        )

    bad = up(b"MZ\x90\x00 not a video")
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_file_type"
    first = up(MP4)
    assert first.status_code == 200 and first.json()["video_url"]
    second = up(MOV)  # replaces the first
    assert second.json()["video_url"] != first.json()["video_url"]
    public = client.get(f"{API}/listings/{car['id']}").json()
    assert public["video_url"] and client.get(public["video_url"]).status_code == 200
    other = client.post(
        f"{API}/listings/{car['id']}/video",
        files={"file": ("v", MP4, "video/mp4")},
        headers=auth_headers(make_user(db)),
    )
    assert other.status_code == 404
    gone = client.delete(f"{API}/listings/{car['id']}/video", headers=h)
    assert gone.status_code == 200 and gone.json()["video_url"] is None


# --- Inspections and test drives (FR-3.4) ----------------------------------------------------
def _ask(client, buyer, listing, **body):
    return client.post(
        f"{API}/listings/{listing['id']}/inquiries", json={"message": "Interested", **body},
        headers=auth_headers(buyer),
    )  # fmt: skip


def test_test_drive_request_and_confirmation(client, db, admin, dealer):
    car = _car(client, admin, dealer)
    buyer = make_user(db)
    asked = _ask(client, buyer, car, kind="test_drive", viewing_at=_future(3).isoformat())
    assert asked.status_code == 201 and asked.json()["kind"] == "test_drive"
    seller_inbox = client.get(f"{API}/me/notifications", headers=auth_headers(dealer)).json()[
        "items"
    ]
    assert seller_inbox[0]["title"] == "Test drive request"

    # The seller accepts but moves it to another time.
    moved = client.post(
        f"{API}/inquiries/{asked.json()['id']}/answer",
        json={"accept": True, "viewing_at": _future(4, 15).isoformat()}, headers=auth_headers(dealer),
    )  # fmt: skip
    assert moved.status_code == 200 and moved.json()["status"] == "accepted"
    assert datetime.fromisoformat(moved.json()["viewing_at"]) == _future(4, 15)
    buyer_inbox = client.get(f"{API}/me/notifications", headers=auth_headers(buyer)).json()["items"]
    assert buyer_inbox[0]["title"] == "Test drive confirmed" and "15:00" in buyer_inbox[0]["body"]
    mine = client.get(f"{API}/me/inquiries", headers=auth_headers(buyer)).json()
    assert [i["kind"] for i in mine] == ["test_drive"]


def test_viewing_request_rules(client, db, admin, dealer):
    car = _car(client, admin, dealer)
    parts_seller = make_user(db, Role.PARTS_PROVIDER, kyc=KycStatus.APPROVED)
    part = _live(
        client,
        admin,
        parts_seller,
        type="part",
        title="Pads",
        price_minor=1,
        attributes={"brand": "X", "condition": "new"},
    )
    buyer = make_user(db)
    codes = {
        "no time": _ask(client, buyer, car, kind="inspection"),
        "past": _ask(client, buyer, car, kind="inspection", viewing_at=_future(-1).isoformat()),
        "part": _ask(client, buyer, part, kind="inspection", viewing_at=_future().isoformat()),
        "time on question": _ask(client, buyer, car, viewing_at=_future().isoformat()),
        "offer without amount": _ask(client, buyer, car, kind="offer"),
    }
    assert {k: (r.status_code, r.json()["error"]["code"]) for k, r in codes.items()} == {
        "no time": (400, "invalid_time"),
        "past": (400, "invalid_time"),
        "part": (422, "vehicles_only"),
        "time on question": (400, "viewing_time_not_allowed"),
        "offer without amount": (400, "offer_required"),
    }
    offer = _ask(client, buyer, car, offer_minor=8_500_000_00)
    assert offer.json()["kind"] == "offer"  # inferred from the amount
    move_offer = client.post(
        f"{API}/inquiries/{offer.json()['id']}/answer",
        json={"accept": True, "viewing_at": _future().isoformat()}, headers=auth_headers(dealer),
    )  # fmt: skip
    assert move_offer.status_code == 400


# --- Views and seller stats (FR-3.5) ---------------------------------------------------------
def test_views_and_stats(client, db, admin, dealer):
    car = _car(client, admin, dealer)
    buyer = make_user(db)
    client.get(f"{API}/listings/{car['id']}")  # anonymous
    client.get(f"{API}/listings/{car['id']}", headers=auth_headers(buyer))
    client.get(
        f"{API}/listings/{car['id']}", headers=auth_headers(dealer)
    )  # the seller: not counted
    _ask(client, buyer, car)
    _ask(client, buyer, car, offer_minor=8_000_000_00)
    _ask(client, buyer, car, kind="inspection", viewing_at=_future().isoformat())

    public = client.get(f"{API}/listings/{car['id']}", headers=auth_headers(buyer)).json()
    assert public["stats"] is None
    mine = client.get(f"{API}/me/listings", headers=auth_headers(dealer)).json()["items"][0]
    assert mine["stats"] == {"views": 3, "enquiries": 3, "offers": 1, "viewing_requests": 1}


# --- Sorting by rating and relevance (FR-8.3) ------------------------------------------------
def test_sort_by_rating_and_relevance(client, db, admin):
    sellers = []
    for name, rating in (("Low Parts", 2.0), ("High Parts", 4.8)):
        s = make_user(db, Role.PARTS_PROVIDER, kyc=KycStatus.APPROVED, business_name=name)
        db.add(RatingSummary(user_id=s.id, average=rating, count=5))
        sellers.append(s)
    db.commit()
    low, high = sellers
    _live(
        client,
        admin,
        low,
        type="part",
        title="Toyota Camry brake pads",
        price_minor=1,
        attributes={"brand": "X", "condition": "new"},
    )
    _live(
        client,
        admin,
        high,
        type="part",
        title="Universal wiper blades",
        price_minor=1,
        attributes={"brand": "X", "condition": "new"},
    )

    by_rating = client.get(f"{API}/listings", params={"type": "part", "sort": "rating"}).json()[
        "items"
    ]
    assert [i["seller"]["name"] for i in by_rating] == ["High Parts", "Low Parts"]

    _live(client, admin, high, type="part", title="Brake pads", price_minor=1, attributes={"brand": "X", "condition": "new"},
          description="Fits many cars, including the Toyota Camry")  # fmt: skip
    relevant = client.get(f"{API}/listings", params={"q": "camry", "sort": "relevance"}).json()[
        "items"
    ]
    assert [i["title"] for i in relevant] == ["Toyota Camry brake pads", "Brake pads"]


# --- Recommendations (FR-8.4) ----------------------------------------------------------------
def test_recommendations_explain_themselves(client, db, admin):
    parts_seller = make_user(db, Role.PARTS_PROVIDER, kyc=KycStatus.APPROVED)
    mechanic = make_user(db, Role.MECHANIC, kyc=KycStatus.APPROVED, city="Lagos")
    insurer = make_user(db, Role.INSURER, kyc=KycStatus.APPROVED, business_name="SafeCover")
    _live(client, admin, parts_seller, type="part", title="Camry pads", price_minor=1,
          fits=[{"make": "Toyota", "model": "Camry"}], attributes={"brand": "X", "condition": "new"})  # fmt: skip
    _live(client, admin, parts_seller, type="part", title="Honda filter", price_minor=1,
          fits=[{"make": "Honda"}], attributes={"brand": "X", "condition": "new"})  # fmt: skip
    _live(
        client,
        admin,
        mechanic,
        type="mechanic_service",
        title="Full service",
        price_minor=1,
        city="Lagos",
        attributes={"category": "servicing"},
    )
    _live(
        client,
        admin,
        insurer,
        type="insurance_product",
        title="Comprehensive",
        price_minor=0,
        attributes={"cover_type": "comprehensive"},
    )

    owner = make_user(db, city="Lagos")
    assert (
        client.get(f"{API}/me/recommendations", headers=auth_headers(owner)).json()["items"] != []
    )  # city alone
    client.post(
        f"{API}/me/vehicles",
        json={"make": "Toyota", "model": "Camry", "year": 2015},
        headers=auth_headers(owner),
    )
    items = client.get(f"{API}/me/recommendations", headers=auth_headers(owner)).json()["items"]
    reasons = {i["listing"]["title"]: i["reason"] for i in items}
    assert reasons["Camry pads"] == "Fits your 2015 Toyota Camry"
    assert "Honda filter" not in reasons
    assert reasons["Full service"] == "Top-rated mechanic services in Lagos"
    assert reasons["Comprehensive"] == "Insurance for your vehicles"


# --- Comparing insurers (FR-7.2) -------------------------------------------------------------
@pytest.fixture
def insurers(client, db, admin):
    out = []
    for name, cover in (
        ("SafeCover", "comprehensive"),
        ("ShieldCo", "comprehensive"),
        ("BasicIns", "third_party"),
    ):
        insurer = make_user(db, Role.INSURER, kyc=KycStatus.APPROVED, business_name=name)
        _live(
            client,
            admin,
            insurer,
            type="insurance_product",
            title=f"{name} {cover}",
            price_minor=0,
            attributes={"cover_type": cover},
        )
        out.append(insurer)
    return out


def test_compare_quotes_from_several_insurers(client, db, insurers):
    safe, shield, basic = insurers
    owner = make_user(db)
    car = client.post(
        f"{API}/me/vehicles",
        json={"make": "Toyota", "model": "Camry", "year": 2018},
        headers=auth_headers(owner),
    ).json()
    r = client.post(
        f"{API}/insurance/quote-requests",
        json={"vehicle_id": car["id"], "details": "Private use", "cover_type": "Comprehensive"},
        headers=auth_headers(owner),
    )
    assert r.status_code == 201, r.text
    group = r.json()["id"]
    apps = {a["insurer_id"]: a for a in r.json()["items"]}
    assert set(apps) == {str(safe.id), str(shield.id)}  # third-party cover not asked
    assert all(a["quote_request_id"] == group for a in apps.values())

    for insurer, premium in ((safe, 150_000_00), (shield, 120_000_00)):
        offer = client.put(
            f"{API}/insurance/applications/{apps[str(insurer.id)]['id']}/offer",
            json={"premium_minor": premium, "terms": "Standard terms"}, headers=auth_headers(insurer),
        )  # fmt: skip
        assert offer.status_code == 200, offer.text
    compared = client.get(
        f"{API}/insurance/quote-requests/{group}", headers=auth_headers(owner)
    ).json()["items"]
    assert [a["premium_minor"] for a in compared] == [120_000_00, 150_000_00]  # cheapest first

    chosen = compared[0]
    accepted = client.post(
        f"{API}/insurance/applications/{chosen['id']}/answer",
        json={"accept": True},
        headers=auth_headers(owner),
    )
    assert accepted.status_code == 200
    after = {
        a["insurer_id"]: a["status"]
        for a in client.get(
            f"{API}/insurance/quote-requests/{group}", headers=auth_headers(owner)
        ).json()["items"]
    }
    assert after == {str(shield.id): "offer_accepted", str(safe.id): "cancelled"}
    told = client.get(f"{API}/me/notifications", headers=auth_headers(safe)).json()["items"]
    assert told[0]["title"] == "Quote request closed"

    assert (
        client.get(
            f"{API}/insurance/quote-requests/{group}", headers=auth_headers(make_user(db))
        ).status_code
        == 404
    )


def test_quote_request_rules(client, db, insurers):
    owner = make_user(db)
    car = client.post(
        f"{API}/me/vehicles",
        json={"make": "Kia", "model": "Rio", "year": 2016},
        headers=auth_headers(owner),
    ).json()

    def ask(**b):
        return client.post(
            f"{API}/insurance/quote-requests",
            json={"vehicle_id": car["id"], **b},
            headers=auth_headers(owner),
        )

    assert ask().json()["error"]["code"] == "cover_required"
    assert ask(cover_type="marine").json()["error"]["code"] == "no_products"
    by_ids = ask(
        product_ids=[
            client.get(
                f"{API}/listings", params={"type": "insurance_product", "q": "BasicIns"}
            ).json()["items"][0]["id"]
        ]
    )
    assert by_ids.status_code == 201 and len(by_ids.json()["items"]) == 1


def test_multi_word_search_matches_words_in_any_order(client, db, admin):
    seller = make_user(db, Role.PARTS_PROVIDER, kyc=KycStatus.APPROVED)
    for title in (
        "Brake pads - Toyota Camry",
        "Brake disc - Honda Accord",
        "100% cotton seat covers",
    ):
        _live(client, admin, seller, type="part", title=title, price_minor=1,
              attributes={"brand": "X", "condition": "new"})  # fmt: skip

    def titles(q):
        return [i["title"] for i in client.get(f"{API}/listings", params={"q": q}).json()["items"]]

    assert titles("brake pads camry") == ["Brake pads - Toyota Camry"]
    assert titles("camry brake") == ["Brake pads - Toyota Camry"]
    assert sorted(titles("brake")) == ["Brake disc - Honda Accord", "Brake pads - Toyota Camry"]
    assert titles("100%") == ["100% cotton seat covers"]  # % is a character, not a wildcard
    assert titles("_") == []
