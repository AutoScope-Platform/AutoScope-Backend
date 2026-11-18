# Project brief

**What:** AutoScope, a Nigerian automotive marketplace: vehicles for sale, spare parts,
mechanic bookings, towing/haulage with SOS, and insurance. Customers (vehicle owners) and
businesses (dealers, parts providers, mechanics, haulage providers, insurers) transact through
the platform; the platform holds payments in escrow and takes a commission.

**This repo:** the backend API only. Clients: a mobile/web app and the admin dashboard
(https://autoscope-admin.netlify.app, currently a front-end mock that defines the admin API).

**Stack (per the technical spec):** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2, Alembic,
PostgreSQL 16 + PostGIS, Redis, Celery, S3-compatible storage, JWT auth, Swagger/OpenAPI.

**Deliberate deviation:** the spec suggests async SQLAlchemy; this build uses the synchronous
API with FastAPI's thread pool. It is simpler to read and debug, and fast enough for the
expected load. Revisit if profiling shows the database layer is the bottleneck.

**Environments:** local (Docker), staging (Render + Neon), production (to be decided).
Staging and production must never share secrets or databases.

**Launch gate (before real users):** live payment keys tested end to end; a KYC vendor
integrated; SMS sender ID approved; Redis set (`REDIS_URL`) and tried with two API instances
for live sockets; backups enabled on the database; first Super admin created; search load test
re-run on staging hardware (locally: p95 under 350 ms at 100,000 listings, see ARCHITECTURE.md).

**Out of scope here:** client apps, marketing site, native push setup on the client side.
