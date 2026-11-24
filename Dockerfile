# One image for the API and the worker (the start command differs; see render.yaml).
FROM python:3.12-slim AS build
# Pinned so a new uv release cannot change or break a build; bump it on purpose.
COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

FROM python:3.12-slim
# libpq is needed by the PostgreSQL driver at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/* && useradd --create-home app \
    && mkdir -p /data/uploads && chown app:app /data/uploads
# /data/uploads: where STORAGE_DRIVER=local keeps files. Created here, owned by the app user,
# so a Docker volume mounted on it starts writable (otherwise Docker creates it owned by root).
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER app
EXPOSE 8000
# Migrations are a release step: on Azure a Container Apps job runs `alembic upgrade head`
# before the new image goes live (docs/AZURE.md). RUN_MIGRATIONS=true runs them at start
# instead (Render, single instance); an advisory lock makes concurrent runs safe either way.
CMD ["sh", "-c", "if [ \"$RUN_MIGRATIONS\" = \"true\" ]; then alembic upgrade head || exit 1; fi; exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --workers ${WEB_CONCURRENCY:-1}"]
