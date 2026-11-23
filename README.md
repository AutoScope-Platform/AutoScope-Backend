# AutoScope Backend

REST API for AutoScope, a marketplace for vehicles, parts, repairs, haulage and insurance.
Python 3.12, FastAPI, PostgreSQL with PostGIS, SQLAlchemy 2.

Interactive API docs: run the app and open `/docs` (Swagger UI) or `/redoc`.

## Quick start

```bash
make install      # install dependencies (needs uv)
make up           # PostGIS + Redis in Docker
cp .env.example .env
make migrate      # create tables
make seed         # demo data
make run          # http://localhost:8000/docs
make test
```

## Try it
After `make seed`, sign in at `POST /api/v1/auth/login` with `adaora@autoscope.ng` /
`AutoScope#2026`, then verify with code `123456` (`/auth/login/verify`). Use the returned
token with the **Authorize** button in Swagger. Other demo accounts are listed in `app/seed.py`.
`make worker` runs the background jobs. `make demo` (with the server running and data seeded)
plays the four main journeys end to end: parts order, mechanic booking, SOS towing and insurance.

## Docs
- `docs/ARCHITECTURE.md`: how the code is organised and the rules it follows
- `docs/INTEGRATIONS.md`: external services, keys to fill in, go-live checklist
- `docs/PROJECT_BRIEF.md`: scope, decisions, launch gate
- `docs/AZURE_WALKTHROUGH.md`: **step by step for the Azure setup call** (what the client clicks, what we run, what you should see)
- `docs/AZURE.md`: the Azure reference behind it, and `scripts/azure_setup.sh` that does the work
- `docs/MOBILE_API_GUIDE.md`: **start here, app developers**: which calls each screen makes, payments, live updates, errors
- `docs/MOBILE_AUTH.md`: sign-up, sign-in (incl. Google/Apple), tokens, account deletion and KYC
- `CONTRIBUTING.md`: how to add to it
