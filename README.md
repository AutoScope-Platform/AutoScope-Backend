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

More: `docs/ARCHITECTURE.md` (how the code is organised) and `CONTRIBUTING.md` (how to add to it).
