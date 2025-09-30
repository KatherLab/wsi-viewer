FROM python:3.13-slim

# Install uv for fast dependency management
RUN pip install --no-cache-dir uv

WORKDIR /app

# Copy project files
COPY pyproject.toml .
COPY uv.lock .
COPY app/ ./app/

# Install dependencies with uv
RUN uv sync --frozen --no-dev

# Create non-root user for security
RUN useradd -m -u 1000 wsi && chown -R wsi:wsi /app
USER wsi

EXPOSE 8010

CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8010", "--workers", "4"]
