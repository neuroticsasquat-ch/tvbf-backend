FROM python:3.13-slim AS base

# UV_PROJECT_ENVIRONMENT: `uv sync` installs into the image's system Python, as
# `uv pip install --system` did, rather than a .venv.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    UV_PROJECT_ENVIRONMENT=/usr/local \
    UV_PYTHON_DOWNLOADS=never

RUN apt-get update \
    && apt-get install -y --no-install-recommends libatomic1 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app

# Install from uv.lock, the set CI tests, not from pyproject.toml's ranges: those
# resolve to whatever is newest at build time, so prod drifted from CI unnoticed
# (sqlalchemy 2.1.5 vs the locked 2.0.52 silently dropped every SQL span).
# --locked fails the build when the lockfile is stale, as it fails CI. --inexact
# keeps what the lockfile doesn't list -- pip and uv itself -- in /usr/local.
COPY pyproject.toml uv.lock ./


FROM base AS dev

RUN uv sync --locked --inexact --no-cache --extra dev --no-install-project

COPY src/ src/
COPY alembic.ini alembic.ini
COPY migrations/ migrations/

RUN uv sync --locked --inexact --no-cache --extra dev

EXPOSE 8000

CMD ["uvicorn", "tvbf.main:app", "--host", "0.0.0.0", "--port", "8000"]


FROM base AS prod

RUN uv sync --locked --inexact --no-cache --no-dev --no-install-project

COPY src/ src/
COPY alembic.ini alembic.ini
COPY migrations/ migrations/

RUN uv sync --locked --inexact --no-cache --no-dev --no-editable

EXPOSE 8000

# opentelemetry-distro defaults the OTLP protocol to gRPC, but we install only
# the HTTP exporter. Pin http/protobuf so the absent grpc exporter is never
# looked up. Endpoint + service.name come from the deploy env (Coolify).
ENV OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf

# Run migrations on startup, then exec uvicorn (wrapped by opentelemetry-instrument
# to auto-instrument FastAPI/SQLAlchemy/asyncpg/httpx) so signals reach the server.
CMD ["sh", "-c", "alembic upgrade head && exec opentelemetry-instrument uvicorn tvbf.main:app --host 0.0.0.0 --port 8000"]
