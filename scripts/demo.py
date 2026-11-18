"""End-to-end demo of the four main journeys from the functional spec, over HTTP.

    make seed && make run          # in one terminal
    python -m scripts.demo         # in another  (BASE_URL defaults to http://localhost:8000)

Each step prints what it did and fails loudly if the API answers with something unexpected,
so it doubles as a smoke test after a deploy. It uses the demo accounts from `app/seed.py`
and the mock payment gateway, so it only works against a database that was seeded and a
server running with the mock drivers (local or staging, never production).

The same `run()` is executed by `tests/test_e2e.py` against an in-process app.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from typing import Any

import httpx

PASSWORD = "AutoScope#2026"
OTP = "123456"
API = "/api/v1"


class Demo:
    """Small helper around an httpx client that keeps one bearer token per demo user."""

    def __init__(self, client: httpx.Client, log=print) -> None:
        self.http = client
        self.log = log
        self.tokens: dict[str, str] = {}
        self.step_no = 0

    # --- plumbing ------------------------------------------------------------------------
    def login(self, email: str) -> None:
        r = self.http.post(f"{API}/auth/login", json={"identifier": email, "password": PASSWORD})
        assert r.status_code == 200, f"login {email}: {r.text}"
        body = r.json()
        if body["requires_otp"]:
            r = self.http.post(
                f"{API}/auth/login/verify",
                json={"challenge_token": body["challenge_token"], "code": OTP},
            )
            assert r.status_code == 200, f"otp {email}: {r.text}"
            body = {"tokens": r.json()}
        self.tokens[email] = body["tokens"]["access_token"]

    def call(
        self, who: str, method: str, path: str, expect: int | tuple[int, ...] = (200, 201), **kw
    ) -> Any:
        headers = {"Authorization": f"Bearer {self.tokens[who]}", **kw.pop("headers", {})}
        r = self.http.request(method, f"{API}{path}", headers=headers, **kw)
        ok = (expect,) if isinstance(expect, int) else expect
        assert r.status_code in ok, f"{method} {path} as {who} -> {r.status_code}: {r.text}"
        return r.json() if r.content and "json" in r.headers.get("content-type", "") else r.text

    def step(self, text: str) -> None:
        self.step_no += 1
        self.log(f"  {self.step_no:>2}. {text}")

    def pay(self, who: str, transaction_id: str, key: str) -> None:
        """Pay through the (mock) gateway the way the app does: start, then verify."""
        paid = self.call(who, "POST", "/payments", json={"transaction_id": transaction_id},
                         headers={"Idempotency-Key": key})  # fmt: skip
        self.call(who, "POST", f"/payments/{paid['reference']}/verify")

    def listing(self, who: str, type_: str, title_contains: str) -> dict:
        items = self.call(who, "GET", f"/listings?type={type_}&page_size=50")["items"]
        return next(i for i in items if title_contains.lower() in i["title"].lower())


# ======================================================================================
# The journeys
# ======================================================================================
def parts_order(d: Demo, admin: str, customer: str, seller: str) -> None:
    d.log("\nJourney 1 - buy spare parts (cart, escrow, delivery, review, payout)")
    pads = d.listing(customer, "part", "brake pads")
    d.step(f"customer finds '{pads['title']}'")
    d.call(customer, "PUT", "/cart/items", json={"listing_id": pads["id"], "quantity": 2})
    out = d.call(customer, "POST", "/checkout", json={"delivery_address": "12 Admiralty Way, Lekki"},
                 headers={"Idempotency-Key": "demo-checkout-1"})  # fmt: skip
    order = out["orders"][0]
    d.step(f"checkout creates order {order['ref']} for ₦{order['amount_minor'] / 100:,.0f}")
    d.pay(customer, order["transaction_id"], "demo-pay-order")
    d.step("customer pays; money is held in escrow")
    d.call(
        seller, "POST", f"/orders/{order['id']}/ship", json={"tracking_note": "Dispatch rider #4"}
    )
    d.step("seller ships")
    done = d.call(customer, "POST", f"/orders/{order['id']}/confirm-received")
    d.step(f"customer confirms receipt -> order is {done['status']}")
    d.call(
        customer,
        "POST",
        "/reviews",
        json={"transaction_ref": order["ref"], "rating": 5, "comment": "Fast delivery"},
    )
    d.step("customer leaves a 5-star review")


def mechanic_booking(d: Demo, customer: str, mechanic: str) -> None:
    d.log("\nJourney 2 - book a mechanic (slots, accept, pay, work, history)")
    service = d.listing(customer, "mechanic_service", "full service")
    mech_id = service["seller"]["id"]
    d.call(mechanic, "PUT", "/me/availability",
           json={"windows": [{"weekday": w, "start_minute": 480, "end_minute": 1020} for w in range(7)]})  # fmt: skip
    day = (datetime.now() + timedelta(days=3)).date()
    slots = d.call(customer, "GET", f"/mechanics/{mech_id}/slots?date={day}&duration_minutes=120")[
        "slots"
    ]
    first = slots[0]
    start = first
    d.step(f"{len(slots)} free slots on {day}; customer picks {start}")
    booking = d.call(customer, "POST", "/bookings",
                     json={"listing_id": service["id"], "scheduled_for": start, "description": "Service due"})  # fmt: skip
    d.step(f"booking {booking['ref']} requested")
    d.call(mechanic, "POST", f"/bookings/{booking['id']}/accept")
    d.step("mechanic accepts")
    d.pay(customer, booking["transaction_id"], "demo-pay-booking")
    d.step("customer pays into escrow")
    d.call(mechanic, "POST", f"/bookings/{booking['id']}/start")
    done = d.call(mechanic, "POST", f"/bookings/{booking['id']}/complete")
    d.step(f"mechanic completes the job -> {done['status']}; invoice and service record created")
    d.call(customer, "GET", f"/bookings/{booking['id']}/invoice")


def sos_haulage(d: Demo, customer: str, provider: str) -> None:
    d.log("\nJourney 3 - SOS towing (estimate, dispatch, accept, live job)")
    pickup = {"latitude": 6.4281, "longitude": 3.4219, "address": "Victoria Island"}
    drop = {"latitude": 6.6018, "longitude": 3.3515, "address": "Ikeja"}
    d.call(
        provider,
        "PUT",
        "/me/haulage/presence",
        json={"available": True, "latitude": 6.4300, "longitude": 3.4200},
    )
    q = "pickup_lat=6.4281&pickup_lng=3.4219&drop_lat=6.6018&drop_lng=3.3515&sos=true"
    est = d.call(customer, "GET", f"/haulage/estimate?{q}")
    d.step(f"SOS estimate: {est}")
    job = d.call(customer, "POST", "/haulage/jobs",
                 json={"pickup": pickup, "drop": drop, "vehicle_description": "Camry, engine failure", "sos": True})  # fmt: skip
    d.step(f"SOS job {job['ref']} created, waiting for payment")
    d.pay(customer, job["transaction_id"], "demo-pay-sos")
    offers = d.call(provider, "GET", "/me/haulage/offers")
    offer = (offers["items"] if isinstance(offers, dict) else offers)[0]
    d.step("payment triggers dispatch; the nearest provider receives the offer")
    d.call(provider, "POST", f"/haulage/offers/{offer['offer_id']}/accept")
    for action in ("start", "pickup", "complete"):
        done = d.call(provider, "POST", f"/haulage/jobs/{job['id']}/{action}")
    d.step(f"provider drives, picks up, delivers -> {done['status']}")


def insurance(d: Demo, customer: str, insurer: str) -> None:
    d.log("\nJourney 4 - insure a car (apply, offer, accept, pay, policy)")
    product = d.listing(customer, "insurance_product", "comprehensive")
    vehicle = d.call(customer, "POST", "/me/vehicles",
                     json={"make": "Toyota", "model": "Corolla", "year": 2017, "plate_number": "LND-482-AB"})  # fmt: skip
    app = d.call(customer, "POST", "/insurance/applications",
                 json={"product_id": product["id"], "vehicle_id": vehicle["id"]})  # fmt: skip
    d.step(f"application {app['ref']} submitted")
    d.call(insurer, "PUT", f"/insurance/applications/{app['id']}/offer",
           json={"premium_minor": 95_000_00, "terms": "Comprehensive, excess ₦20,000"})  # fmt: skip
    d.step("insurer sends a ₦95,000 offer")
    accepted = d.call(
        customer, "POST", f"/insurance/applications/{app['id']}/answer", json={"accept": True}
    )
    d.pay(customer, accepted["transaction_id"], "demo-pay-policy")
    d.step("customer accepts and pays")
    policies = d.call(customer, "GET", "/me/policies")
    policy = (policies["items"] if isinstance(policies, dict) else policies)[0]
    d.step(f"policy {policy['policy_number']} issued automatically")
    d.call(customer, "GET", f"/insurance/policies/{policy['id']}/certificate")


def admin_tour(d: Demo, admin: str) -> None:
    d.log("\nAdmin - review queue, overview, escrow release, audit")
    pending = d.call(admin, "GET", "/admin/listings?status=pending")
    rows = pending["items"] if isinstance(pending, dict) else pending
    if rows:
        d.call(admin, "POST", f"/admin/listings/{rows[0]['id']}/decision", json={"approve": True})
        d.step(f"approved pending listing '{rows[0]['title']}'")
    d.call(admin, "POST", "/admin/finance/jobs/release-escrow")
    o = d.call(admin, "GET", "/admin/overview")
    d.step(
        f"overview: GMV ₦{o['gmv_minor'] / 100:,.0f}, escrow held ₦{o['escrow_held_minor'] / 100:,.0f}, queues {o['queues']}"
    )
    log = d.call(admin, "GET", "/admin/audit")
    d.step(f"audit log has {log['total']} entries")


def run(client: httpx.Client, log=print) -> None:
    d = Demo(client, log)
    accounts = {
        "admin": "adaora@autoscope.ng", "customer": "kunle@demo.ng", "buyer2": "chioma@demo.ng",
        "parts": "ngozi.parts@demo.ng", "mechanic": "ola.motors@demo.ng",
        "haulage": "fastlane@demo.ng", "insurer": "cover@demo.ng",
    }  # fmt: skip
    for email in accounts.values():
        d.login(email)
    c, p, m, h, i, a = (
        accounts[k] for k in ("customer", "parts", "mechanic", "haulage", "insurer", "admin")
    )
    parts_order(d, a, c, p)
    mechanic_booking(d, c, m)
    sos_haulage(d, c, h)
    insurance(d, c, i)
    admin_tour(d, a)
    d.log("\nAll journeys completed.")


if __name__ == "__main__":
    # Windows consoles default to a code page without "₦"; print UTF-8 everywhere.
    sys.stdout.reconfigure(encoding="utf-8")
    base = os.environ.get("BASE_URL", "http://localhost:8000")
    with httpx.Client(base_url=base, timeout=30) as http:
        try:
            run(http)
        except AssertionError as error:
            sys.exit(f"\nDEMO FAILED: {error}")
