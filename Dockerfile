FROM python:3.13-slim

RUN pip install --no-cache-dir uv

WORKDIR /app

COPY pyproject.toml .
COPY uv.lock .
COPY app/ ./app/

RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

RUN useradd -m -u 1000 wsi && chown -R wsi:wsi /app
USER wsi

EXPOSE 8010

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8010", "--workers", "4"]
