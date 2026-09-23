# Dockerfile for 24/7 Cloud Hosting of Autoposter
FROM python:3.11-slim

# Prevent interactive prompts and buffering
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install system dependencies (FFmpeg, fonts, build tools)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fonts-liberation \
    fonts-dejavu-core \
    git \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browser and system OS dependencies for headless Chromium
RUN python -m playwright install --with-deps chromium

# Copy application files
COPY . .

# Ensure data, logs, assets, and tmp directories exist
RUN mkdir -p data logs assets/gameplay tmp

# Run the Discord bot
CMD ["python", "main.py"]
