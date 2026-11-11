# Architecture

A **modular monolith**: one deployable FastAPI app, split into modules that each own their
tables. Background jobs run in a second process from the same code (`app/worker.py`).

```
app/
  main.py            builds the app, registers routers, error handlers, /health
  core/              shared plumbing: config, db, errors, security, permissions (RBAC),
                     pagination, events, idempotency, rate limits, audit log, files, realtime
  integrations/      one driver per outside service + a mock for each (see docs/INTEGRATIONS.md)
  modules/
    identity/        accounts, OTP, login, roles, KYC submissions
    catalog/         vehicles, listings (vehicle/part/mechanic/insurance), search, review queue
    orders/          parts cart + checkout, vehicle purchase, shipping
    booking/         mechanic availability, bookings, quotes, invoices
    haulage/         fare estimate, SOS, dispatch, live tracking
    insurance/       applications, offers, policies, claims
    payments/        the money ledger, escrow, commission, wallet, payouts, disputes
    reviews/         ratings, replies, abuse reports
    messaging/       buyer-seller chat (REST + WebSocket)
    notifications/   inbox, push/SMS/e-mail rules, announcements
    admin/           overview KPIs, KYC queue, users, audit log, provider analytics
  worker.py          Celery jobs
  seed.py            demo data
migrations/          Alembic (one baseline migration)
tests/               pytest, runs on a real PostGIS database
```

Each module has the same four files: `models.py` (tables), `schemas.py` (request/response
bodies, these drive Swagger), `service.py` (business rules, **all logic lives here**),
`router.py` (HTTP only: parse, call a service function, return).

## Rules that keep it maintainable

1. **Routers are thin.** If you are writing an `if` about business rules in a router, move it to
   the service.
2. **Modules do not reach into each other's tables.** Call the other module's `service`
   functions. One documented exception: `admin` reads other tables for reports (read only).
3. **Services commit once, at the end.** Helpers inside a service only `flush`.
4. **Money is an integer in kobo** (`*_minor`). Never use floats for money.
5. **Every sale is one `Transaction` row** (`payments` module): parts order (ORD), vehicle
   (VEH), booking (BKG), haulage job (JOB), policy (POL). Orders, bookings, jobs and policies
   store their own details and point at the transaction; they never keep their own payment
   status.
6. **Money writes take an `Idempotency-Key`** header so a retry cannot charge twice.
7. **Side effects go through events.** After a commit a service calls `publish("payment.succeeded", ...)`.
   Other modules `subscribe`. That is how a payment confirms an order, issues a policy and
   triggers notifications without the modules importing each other.

## Money flow (escrow)

`pay -> held in escrow -> work completed -> dispute window (hold_days, default 3) -> released
to the provider's wallet minus commission -> payout on the schedule`.
A dispute freezes the escrow until an admin resolves it (refund buyer / release provider / split).
Commission is copied onto the transaction at payment time, so changing the rate later never
changes past sales.

## Data and search

PostgreSQL 16 with **PostGIS**. Listings and provider locations have a `geography(Point,4326)`
column plus plain `latitude`/`longitude`. Distance search uses `ST_DWithin` / `ST_Distance`.
Extensions `postgis`, `pg_trgm`, `pgcrypto` are created by the baseline migration.

## Real time

`core/realtime.py` is a small in-process hub used by the haulage tracking socket and the chat
socket. **It works in one process only.** Before running more than one API instance, replace
the hub's fan-out with Redis pub/sub (the interface is two methods).

## Background jobs

| Job | When | What |
| --- | --- | --- |
| haulage redispatch | every minute | expire stale offers, widen search for unassigned jobs |
| announcements | every minute | send scheduled announcements |
| escrow release | every 10 min | release funds whose dispute window passed |
| order auto-confirm | every 10 min | complete orders shipped 7+ days ago |
| payouts | daily 06:00 UTC | schedule payouts from wallets, pay those due |
| policy lifecycle | daily 06:15 UTC | expire ended policies, remind 30 days ahead |

Each job calls one service function, so tests call the function directly.

## Security summary

JWT access (15 min) + rotating refresh tokens; passwords hashed; OTP on sign-up and for
staff/business sign-in; account lock after repeated failures; role checks via capabilities
(`core/permissions.py`, mirrors the FSD matrix); rate limits on auth and chat; KYC documents
served by short-lived links; every admin action is written to the audit log.
