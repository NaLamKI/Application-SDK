# Base image for extensions: Python 3.12, the appext SDK with uvicorn, an unprivileged user.
# Build it with build.sh (it builds the wheel this file installs).
FROM python:3.12-slim

ARG APPEXT_WHEEL
LABEL org.opencontainers.image.title="appext base image" \
      org.opencontainers.image.description="Python 3.12 with the appext SDK for extensions of platforms that follow the reference architecture"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8000

RUN groupadd --system --gid 10001 appext \
 && useradd --system --uid 10001 --gid appext --create-home --shell /usr/sbin/nologin appext

# The SDK comes from the wheel in the build context, not from an index: the image
# contains exactly the version that was built next to it.
COPY ${APPEXT_WHEEL} /tmp/
RUN test -n "${APPEXT_WHEEL}" \
 && pip install "/tmp/${APPEXT_WHEEL}[redis]" \
 && rm -f /tmp/*.whl

WORKDIR /app
# Unprivileged by default. An extension's Dockerfile switches to root for its own
# `pip install` and back to appext afterwards.
USER appext
EXPOSE 8000
# `appext health` asks the process itself: GET http://127.0.0.1:$PORT/healthz
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD ["appext", "health"]
# Proxy headers are believed only from the addresses in APPEXT_TRUSTED_PROXIES.
CMD ["appext", "serve", "app.main:app"]
