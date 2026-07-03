FROM python:3.13-slim

# gosu is used by the entrypoint to drop from root to the wsi user after
# starting the root aclcheckd daemon. ca-certificates is needed for LDAPS TLS
# validation against FreeIPA.
RUN apt-get update && apt-get install -y --no-install-recommends \
      gosu ca-certificates && \
    rm -rf /var/lib/apt/lists/* && \
    pip install --no-cache-dir uv

WORKDIR /app

COPY pyproject.toml .
COPY uv.lock .
COPY app/ ./app/

RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

RUN useradd -m -u 1000 wsi && chown -R wsi:wsi /app

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# NOTE: the container runs as root so the entrypoint can start aclcheckd (which
# needs CAP_SETUID/GID to impersonate users). The entrypoint drops to wsi for
# uvicorn itself; only aclcheckd remains root.
EXPOSE 8010

ENTRYPOINT ["/entrypoint.sh"]
