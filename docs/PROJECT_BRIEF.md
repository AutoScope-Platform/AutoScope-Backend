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
integrated; SMS sender ID approved; Redis pub/sub for sockets if more than one API instance;
backups enabled on the database; first Super admin created; load test on search.

**Out of scope here:** client apps, marketing site, native push setup on the client side.
