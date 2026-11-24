# Load test of staging, 24 November 2026

How many app users at once the staging setup can serve, and what gives way first.
`scripts/loadtest_staging.py` (see its docstring for how to run it).

## Setup

- **Staging:** one Azure B2s VM (2 vCPUs, 4 GB): Caddy, the API (2 processes), the worker,
  PostgreSQL and Redis all on it. Only the 8 demo listings, so this measures the server setup,
  not search over a big catalogue (that was tested separately: p95 under 350 ms at 100,000
  listings, docs/ARCHITECTURE.md).
- **Virtual users** behave like app users: search and filters, near-me, a listing and its
  seller's reviews, mechanics and their free slots, haulage estimates, profile, notifications,
  unread messages, and the cart (add, view, remove), 1-3 seconds between screens. Each makes
  about one request a second, far busier than a real person, who pauses to read.
- Run twice: from a developer's computer over the internet (realistic network, but that
  connection tops out near 50 requests/second), and from inside the VM (no network limit, but
  the test then shares the VM's 2 CPUs, so the results understate capacity a little).

## What it found, and what was fixed

1. **The API locked up under heavy load and stayed down** (fixed). At 200+ users at once,
   requests holding a database connection waited for a worker thread while every thread
   waited for a connection. Nothing moved; even `/health` stopped answering, and the API
   stayed stuck after the load ended until it was restarted. Fix (`app/core/concurrency.py`):
   each process works on at most half as many requests as it has connections; the rest queue
   without holding a thread or a connection, and get `503 service_busy` after 10 seconds.
   `/health` skips the queue. Staging now has 10+10 connections per API process.
2. **Adding the same part to the cart twice at once answered 500** (fixed): a double tap or
   two phones. Adding and removing are now single database statements, safe to repeat.

## Results after the fixes (from inside the VM)

| Users at once | Requests/s | Typical (p50) | p95 | Errors |
| --- | --- | --- | --- | --- |
| 50 | 44 | 23 ms | 55 ms | 0 |
| 100 | 88 | 39 ms | 177 ms | 0 |
| 200 | 70 | 1.1 s | 5.5 s | 0 |
| 300 | 61 | 2.9 s | 11.8 s | 3 (connection drops on the test side) |
| 400 | 66 | 3.6 s | 14.8 s | 0 |

All 21,017 answers the API gave were successful (no 500s, no connection-pool errors), and
right after the test it answered at normal speed (`/health` 50 ms, a search 70 ms).

**In plain terms:** staging stays fast up to about **90 requests a second**, roughly 100 of
these very busy virtual users. Real people pause between taps, so that is several hundred
people using the app at the same moment. Past that it slows down (the VM's 2 CPUs are the
limit) but no longer fails, and recovers by itself as soon as traffic drops.

## For production

- More CPUs (or more API instances, as on Container Apps) raise the ~90 requests/s ceiling
  roughly in proportion: the database was never the limit here (under 25% CPU at peak).
- Keep (connections per process x processes) under the database's limit; the request cap
  follows the pool size by itself (`MAX_CONCURRENT_REQUESTS` overrides it).
- Repeat this test on the production setup before launch (docs/ROADMAP.md, item 20).
