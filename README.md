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
- `CONTRIBUTING.md`: how to add to it

More: `docs/ARCHITECTURE.md` (how the code is organised) and `CONTRIBUTING.md` (how to add to it).
