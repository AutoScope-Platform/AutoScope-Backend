# Working on this codebase

## Setup
```bash
make install && make up && cp .env.example .env
make migrate && make seed && make run     # http://localhost:8000/docs
make test                                  # needs the database from `make up`
```
Without Docker, install PostgreSQL 16 + PostGIS and set `DATABASE_URL` (and
`TEST_DATABASE_URL` for tests, a separate empty database).

## Daily commands
`make run`, `make test`, `make lint`, `make format`, `make worker`, `make help`.

## Adding a feature (the recipe)
1. Pick the module (or create `app/modules/<name>/` with `models.py schemas.py service.py router.py`).
2. Write the rule in `service.py`, with a docstring saying *why*.
3. Add request/response schemas, then a thin route with `summary`, `responses=error_responses(...)`.
4. New module? Import its models in `app/models.py` and its router in `app/main.py` (ROUTERS and TAGS).
5. Change a table? `make migration m="what changed"`, read the generated file, then `make migrate`.
   Never edit a migration that has been applied anywhere shared; add a new one.
6. Add tests in `tests/test_<module>.py` (see any existing file; `make_user` and `auth_headers`
   in `tests/factories.py` do the set-up).
7. `make lint && make test`.

## Conventions
- Comments explain **why**, not what. Public functions, schemas and routes have docstrings,
  because they become the Swagger text.
- Errors: raise `NotFound`, `Conflict`, `Forbidden`, `BadRequest` from `app/core/errors.py`
  with a stable `code=`. The client reads the code, the user reads the message.
- Money: integer kobo, fields named `*_minor`.
- Times: stored UTC; Nigerian local time (UTC+1) only for display or day boundaries.
- New outside service? Add a driver + a mock (`app/integrations`), never call a vendor from a service.
- Commits: small, present tense ("Add dispute resolution"). Branch from `develop`, open a PR.

## When something breaks
- Postgres not running: `make up` (Docker) or start your local service.
- A test fails only after a model change: the test database is rebuilt every run, so it is a
  real failure, not leftovers.
- `alembic check` complains: your models and migrations differ; generate a migration.
