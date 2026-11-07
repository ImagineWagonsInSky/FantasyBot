# syntax=docker/dockerfile:1
#
# FantasyBot image. Runs on x86_64 laptops and on 64-bit Raspberry Pi (arm64).
#
# The base image is pinned by its multi-platform digest so every build starts
# from the same bytes. To move to a newer Python 3.13 patch release, run
#   docker buildx imagetools inspect python:3.13-slim
# and replace the digest with the top-level "Digest:" it prints.
ARG PYTHON_IMAGE=python:3.13-slim@sha256:09a17cad02668ae61ece136b4fd36ac3849e14f670ebedb637ea3e15580525bc
FROM ${PYTHON_IMAGE}

LABEL org.opencontainers.image.title="FantasyBot" \
      org.opencontainers.image.description="LaLiga Fantasy automation. Includes ‘LaLigaApp’ (c) 2025–present Externoak, https://github.com/Externoak/LaLigaApp/" \
      org.opencontainers.image.licenses="GPL-3.0-or-later" \
      org.opencontainers.image.source="https://github.com/ImagineWagonsInSky/FantasyBot"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Dependencies: prebuilt wheels only (no package build scripts run), and every
# file is checked against the hash in requirements.lock.
COPY requirements.lock /tmp/requirements.lock
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --require-hashes --no-deps --only-binary=:all: -r /tmp/requirements.lock \
 && rm /tmp/requirements.lock

# The bot itself runs from source; nothing is built or installed.
COPY LICENSE NOTICE /app/
COPY src/ /app/src/
RUN /opt/venv/bin/python -m compileall -q /app/src \
 && groupadd --system --gid 10001 fantasybot \
 && useradd --system --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent \
            --shell /usr/sbin/nologin fantasybot \
 && install -d -o 10001 -g 10001 -m 700 /state /secrets

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app/src \
    FANTASYBOT_STATE_DIR=/state

# Never root at runtime. Code and dependencies are owned by root, so the bot
# can't change them; it can only write /state and (during setup) /secrets.
USER 10001:10001
WORKDIR /state
ENTRYPOINT ["python", "-m", "fantasybot"]
CMD ["healthcheck"]
