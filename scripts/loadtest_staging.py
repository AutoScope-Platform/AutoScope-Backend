"""Load test of a running deployment (staging): many app users at once, over the network.

    python -m scripts.loadtest_staging --base https://20-106-252-207.sslip.io --stages 10,25,50,100,150 --stage-seconds 120

Each virtual user behaves like someone using the mobile app: signs in as one of the demo
accounts (shared, signed in once each), then loops through home, search, filters, near-me,
a listing and its seller's reviews, mechanics and their free slots, a haulage estimate,
notifications and unread messages, and the cart (add a part, then take it out again), with
1-3 seconds between screens. It never creates orders, bookings or anything another user
would see, so staging data is left as it was.

The number of users steps up stage by stage. For each stage it prints requests per second,
p50 / p95 / p99 response times, and errors; at the end, the slowest endpoints. Times are
measured here, so they include the network: compare with the `/health` round trip printed
first. Uses only httpx (no compiled packages), so it runs anywhere the project runs.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import statistics
import time
from collections import defaultdict
from datetime import date, timedelta

import httpx

PASSWORD = "AutoScope#2026"
CODE = "123456"
CUSTOMERS = ["kunle@demo.ng"]
OTHERS = ["lekki.autos@demo.ng", "ngozi.parts@demo.ng", "ola.motors@demo.ng", "fastlane@demo.ng"]

SEARCHES = [
    {},
    {"q": "brake"},
    {"q": "toyota camry", "sort": "relevance"},
    {"type": "vehicle", "sort": "price_asc"},
    {"type": "part", "city": "Lagos"},
    {"lat": 6.5244, "lng": 3.3792, "radius_km": 25, "sort": "distance"},
    {"fits_make": "Toyota", "fits_model": "Camry", "fits_year": 2016},
]


class Stats:
    def __init__(self) -> None:
        self.times: dict[str, list[float]] = defaultdict(list)
        self.errors: dict[str, int] = defaultdict(int)
        self.error_samples: dict[str, str] = {}

    def add(self, name: str, seconds: float, ok: bool, detail: str = "") -> None:
        self.times[name].append(seconds)
        if not ok:
            self.errors[name] += 1
            self.error_samples.setdefault(name, detail)

    def all_times(self) -> list[float]:
        return [t for ts in self.times.values() for t in ts]


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))]


async def sign_in(client: httpx.AsyncClient, email: str) -> str:
    r = await client.post("/api/v1/auth/login", json={"identifier": email, "password": PASSWORD})
    r.raise_for_status()
    out = r.json()
    if out.get("requires_otp"):
        r = await client.post(
            "/api/v1/auth/login/verify",
            json={"challenge_token": out["challenge_token"], "code": CODE},
        )
        r.raise_for_status()
        out = r.json()
    return (out.get("tokens") or out)["access_token"]


async def call(
    client, stats: Stats, name: str, method: str, path: str, **kw
) -> httpx.Response | None:
    start = time.perf_counter()
    try:
        r = await client.request(method, path, **kw)
    except httpx.HTTPError as exc:
        stats.add(name, time.perf_counter() - start, False, type(exc).__name__)
        return None
    ok = r.status_code < 400
    stats.add(name, time.perf_counter() - start, ok, f"{r.status_code} {r.text[:120]}")
    return r


async def user_loop(
    client, stats_ref, token: str, data: dict, is_customer: bool, stop: asyncio.Event
):
    auth = {"Authorization": f"Bearer {token}"}
    rnd = random.Random()
    while not stop.is_set():
        s: Stats = stats_ref[0]
        step = rnd.random()
        if step < 0.30:
            await call(
                client,
                s,
                "GET /listings (search)",
                "GET",
                "/api/v1/listings",
                params={**rnd.choice(SEARCHES), "limit": 20},
            )
        elif step < 0.45:
            lst = rnd.choice(data["listings"])
            await call(client, s, "GET /listings/{id}", "GET", f"/api/v1/listings/{lst['id']}")
            await call(
                client,
                s,
                "GET /users/{id}/reviews",
                "GET",
                f"/api/v1/users/{lst['seller']['id']}/reviews",
            )
        elif step < 0.55:
            await call(
                client,
                s,
                "GET /mechanics",
                "GET",
                "/api/v1/mechanics",
                params=rnd.choice([{}, {"lat": 6.5244, "lng": 3.3792, "sort": "distance"}]),
            )
            if data["mechanics"]:
                day = (date.today() + timedelta(days=rnd.randint(1, 6))).isoformat()
                await call(
                    client,
                    s,
                    "GET /mechanics/{id}/slots",
                    "GET",
                    f"/api/v1/mechanics/{rnd.choice(data['mechanics'])}/slots",
                    params={"date": day},
                )
        elif step < 0.60:
            await call(
                client,
                s,
                "GET /haulage/estimate",
                "GET",
                "/api/v1/haulage/estimate",
                params={"pickup_lat": 6.45, "pickup_lng": 3.39, "drop_lat": 6.60, "drop_lng": 3.35},
            )
        elif step < 0.80:
            await call(client, s, "GET /me", "GET", "/api/v1/me", headers=auth)
            await call(
                client, s, "GET /me/notifications", "GET", "/api/v1/me/notifications", headers=auth
            )
            await call(
                client,
                s,
                "GET /threads/unread-count",
                "GET",
                "/api/v1/threads/unread-count",
                headers=auth,
            )
        elif step < 0.88:
            await call(
                client,
                s,
                "GET /me/recommendations",
                "GET",
                "/api/v1/me/recommendations",
                headers=auth,
            )
        elif is_customer and data["cart_part"]:
            part = data["cart_part"]
            await call(
                client,
                s,
                "PUT /cart/items (add)",
                "PUT",
                "/api/v1/cart/items",
                json={"listing_id": part, "quantity": 1},
                headers=auth,
            )
            await call(client, s, "GET /cart", "GET", "/api/v1/cart", headers=auth)
            await call(
                client,
                s,
                "PUT /cart/items (remove)",
                "PUT",
                "/api/v1/cart/items",
                json={"listing_id": part, "quantity": 0},
                headers=auth,
            )
        else:
            await call(
                client, s, "GET /listings (search)", "GET", "/api/v1/listings", params={"limit": 20}
            )
        try:
            await asyncio.wait_for(stop.wait(), timeout=rnd.uniform(1.0, 3.0))
        except TimeoutError:
            pass


async def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--base", required=True)
    ap.add_argument("--stages", default="10,25,50,100,150", help="virtual users per stage")
    ap.add_argument("--stage-seconds", type=int, default=120)
    args = ap.parse_args()
    stages = [int(x) for x in args.stages.split(",")]

    limits = httpx.Limits(
        max_connections=max(stages) + 20, max_keepalive_connections=max(stages) + 20
    )
    async with httpx.AsyncClient(base_url=args.base, timeout=30, limits=limits) as client:
        rtt = []
        for _ in range(10):
            t = time.perf_counter()
            (await client.get("/health")).raise_for_status()
            rtt.append(time.perf_counter() - t)
        print(f"network round trip (/health, idle): p50 {statistics.median(rtt) * 1000:.0f} ms\n")

        tokens = {e: await sign_in(client, e) for e in CUSTOMERS + OTHERS}
        listings = (await client.get("/api/v1/listings", params={"limit": 50})).json()["items"]
        mechanics = [m["id"] for m in (await client.get("/api/v1/mechanics")).json()["items"]]
        cust = {"Authorization": f"Bearer {tokens[CUSTOMERS[0]]}"}
        cart = (await client.get("/api/v1/cart", headers=cust)).json()
        in_cart = {line["listing_id"] for line in cart["items"]}
        parts = [
            x["id"]
            for x in listings
            if x["type"] == "part" and (x.get("stock") or 0) > 0 and x["id"] not in in_cart
        ]
        data = {
            "listings": listings,
            "mechanics": mechanics,
            "cart_part": parts[0] if parts else None,
        }
        print(
            f"{len(listings)} listings, {len(mechanics)} mechanics; cart test part: {data['cart_part']}\n"
        )

        stats_ref = [Stats()]
        stop = asyncio.Event()
        tasks: list[asyncio.Task] = []
        overall = Stats()
        print(f"{'users':>5} {'req/s':>6} {'p50 ms':>7} {'p95 ms':>7} {'p99 ms':>7} {'errors':>7}")
        for users in stages:
            stats_ref[0] = Stats()
            while len(tasks) < users:
                i = len(tasks)
                email = CUSTOMERS[0] if i % 2 == 0 else OTHERS[(i // 2) % len(OTHERS)]
                tasks.append(
                    asyncio.create_task(
                        user_loop(client, stats_ref, tokens[email], data, email in CUSTOMERS, stop)
                    )
                )
                await asyncio.sleep(0.05)
            t0 = time.perf_counter()
            await asyncio.sleep(args.stage_seconds)
            s = stats_ref[0]
            times = s.all_times()
            n_err = sum(s.errors.values())
            print(
                f"{users:>5} {len(times) / (time.perf_counter() - t0):>6.1f} {pct(times, 50) * 1000:>7.0f} "
                f"{pct(times, 95) * 1000:>7.0f} {pct(times, 99) * 1000:>7.0f} {n_err:>7}",
                flush=True,
            )
            for k, v in s.times.items():
                overall.times[k] += v
            for k, v in s.errors.items():
                overall.errors[k] += v
                overall.error_samples.setdefault(k, s.error_samples[k])
        stop.set()
        await asyncio.gather(*tasks)

        if data["cart_part"]:  # leave the cart exactly as it was
            await client.put(
                "/api/v1/cart/items",
                json={"listing_id": data["cart_part"], "quantity": 0},
                headers=cust,
            )

        print("\nper endpoint (all stages):")
        print(f"  {'endpoint':<30} {'calls':>6} {'p50 ms':>7} {'p95 ms':>7} {'errors':>6}")
        for name, ts in sorted(overall.times.items(), key=lambda kv: -pct(kv[1], 95)):
            print(
                f"  {name:<30} {len(ts):>6} {pct(ts, 50) * 1000:>7.0f} {pct(ts, 95) * 1000:>7.0f} {overall.errors[name]:>6}"
            )
        for name, sample in overall.error_samples.items():
            print(f"  error sample, {name}: {sample}")


if __name__ == "__main__":
    asyncio.run(main())
