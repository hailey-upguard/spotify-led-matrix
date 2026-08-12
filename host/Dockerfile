# syntax=docker/dockerfile:1

# Build stage: resolve deps into a venv so the runtime image gets site-packages
# without pip, setuptools, wheel or any of their metadata.
FROM python:3.12-alpine AS builder

# --without-pip matters: a normal venv bundles its own pip, which is 13MB of
# runtime image that only ever gets used at build time. Install with the
# builder's pip against the venv prefix instead.
RUN python -m venv --without-pip /venv

COPY requirements.txt .
# Alpine means musl, so this depends on musllinux wheels existing for Pillow
# (they do, 10.x onward). There is no compiler in this image on purpose: if a
# wheel ever goes missing the build fails loudly instead of quietly growing a
# toolchain.
RUN pip install --no-cache-dir --only-binary :all: --prefix /venv -r requirements.txt

FROM python:3.12-alpine

# No apt/apk runtime libs needed: the Pillow wheel vendors its own libjpeg and
# libpng under site-packages/pillow.libs, which is what the old
# libjpeg62-turbo/zlib1g install was duplicating.
COPY --from=builder /venv /venv
ENV PATH="/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY spotify_matrix/ ./spotify_matrix/

RUN adduser -D -H appuser
USER appuser

# /healthz and /readyz (see spotify_matrix/health.py).
EXPOSE 8080

ENTRYPOINT ["python", "-m", "spotify_matrix.main"]
