FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

RUN apt-get update \
    && apt-get install -y --no-install-recommends libatomic1 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app

COPY pyproject.toml ./
RUN uv pip install --system --no-cache ".[dev]"

COPY src/ src/
COPY alembic.ini alembic.ini
COPY migrations/ migrations/

EXPOSE 8000

CMD ["uvicorn", "tvbf.main:app", "--host", "0.0.0.0", "--port", "8000"]
