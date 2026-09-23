# Boterdrop Solver — Camoufox (Firefox) + Playwright Chromium (reCAPTCHA v3)
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:99

RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb \
        ca-certificates \
        fonts-liberation \
        libgtk-3-0 \
        libdbus-glib-1-2 \
        libxt6 \
        libx11-xcb1 \
        libasound2 \
        libnss3 \
        libgbm1 \
        libxshmfence1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt \
    && python -m camoufox fetch \
    && python -m playwright install --with-deps chromium

COPY api_server.py run_server.py config.docker.json config.json proxies.docker.txt proxies.txt ./

EXPOSE 20011

# RAM cap is applied at container level (docker --memory), not here.
CMD ["sh", "-c", "Xvfb :99 -screen 0 1280x1024x24 -nolisten tcp & exec python run_server.py"]
