FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install .

RUN useradd --system --uid 10001 --create-home --home-dir /home/ttcal ttcal \
    && mkdir -p /config /data \
    && chown ttcal:ttcal /config /data

USER ttcal

VOLUME ["/config", "/data"]

# Configuration is read from /config/ttcal.toml (mount it read-only).
# Set state_db = "/data/ttcal.db" so the database survives the container.
ENTRYPOINT ["ttcal", "-c", "/config/ttcal.toml"]
CMD ["run"]
