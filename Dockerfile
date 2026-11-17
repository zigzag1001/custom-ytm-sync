# ---------------------------------------------------------------------------
# ytmusic-dl — YouTube Music downloader with yt-dlp + bgutil PO token support
# ---------------------------------------------------------------------------
# Build:  docker build -t ytmusic-dl .
# Run:    docker run --rm --env-file .env \
#           -v /your/music:/music \
#           --network host \           # or use docker-compose network
#           ytmusic-dl
# ---------------------------------------------------------------------------

FROM python:3.12-slim

# ---------------------------------------------------------------------------
# System dependencies
#   ffmpeg     — audio extraction/conversion (required by yt-dlp)
#   curl/unzip — used to install Deno
# ---------------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        unzip \
        rsgain \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# Deno (recommended JS runtime for yt-dlp EJS challenge solving)
# Installed to /usr/local/bin/deno so it's on PATH for all users.
# ---------------------------------------------------------------------------
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh

# ---------------------------------------------------------------------------
# Python dependencies
# ---------------------------------------------------------------------------
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------
# COPY py/dl.py .

# Output directory — mount your music library here
VOLUME ["/music"]
# RUN chown -R 1000:1000 /music

# Default command — runs the downloader once then exits (suited for cron)
# CMD ["python", "-u", "dl.py"]
