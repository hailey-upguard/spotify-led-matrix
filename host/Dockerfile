FROM python:3.12-slim

# Pillow needs a couple of runtime libs for jpeg/zlib decoding.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libjpeg62-turbo zlib1g \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY spotify_matrix/ ./spotify_matrix/

# Run as non-root.
RUN useradd --create-home appuser
USER appuser

ENTRYPOINT ["python", "-m", "spotify_matrix.main"]
