# syntax=docker/dockerfile:1.7
#
# Single-image build for on-premise deployment.
#
# Stage 1: build the Next.js app with Node 22.
# Stage 2: assemble runtime image based on the official Playwright Python image
#          (ships with Chromium/Firefox/Webkit + all required OS deps).
#          We layer Node 22 and the Codex CLI on top.

# ---------- Stage 1: Next.js build ----------
FROM node:22-bookworm-slim AS web-build

WORKDIR /app/finolaw

# Install deps with a cached lockfile for reproducibility.
COPY finolaw/package.json finolaw/package-lock.json ./
RUN npm ci

# Copy the rest of the finolaw sources and produce a production build.
COPY finolaw/ ./
RUN npx next build


# ---------- Stage 2: Runtime ----------
# Playwright's official Python image includes browsers + OS deps pre-installed.
# It currently ships with Node 20, so we upgrade to Node 22 for Next.js 16.
FROM mcr.microsoft.com/playwright/python:v1.48.0-noble

# Install Node 22 (replacing the bundled Node), plus supervisor/cron/curl.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates gnupg supervisor cron \
 && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*

# Install Codex CLI globally. Package is @openai/codex (installs binary at /usr/local/bin/codex).
RUN npm install -g @openai/codex

# --- Python deps ---
WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# --- Project source ---
COPY crawler/ /app/crawler/
COPY api/     /app/api/
COPY scripts/ /app/scripts/

# --- Next.js build artefacts + runtime deps ---
# Copy the entire built finolaw/ tree (including node_modules so `next start` works).
COPY --from=web-build /app/finolaw /app/finolaw

# Supervisor config drives the long-running Next.js process.
# Crawlers are spawned on-demand by finolaw as short-lived Python subprocesses.
COPY docker/supervisord.conf /etc/supervisor/conf.d/app.conf

# finolaw's lib/crawler-runner.ts expects .venv/bin/python — create a symlink
# that points to the system Python so the existing code runs unchanged.
RUN mkdir -p /app/.venv/bin \
 && ln -sf /usr/bin/python3 /app/.venv/bin/python \
 && ln -sf /usr/bin/python3 /app/.venv/bin/python3

ENV PYTHONUNBUFFERED=1 \
    NODE_ENV=production \
    PORT=3001 \
    PATH="/app/.venv/bin:/usr/local/bin:/usr/bin:/bin"

EXPOSE 3001

# Persisted at runtime via bind mounts (see docker-compose.yml).
#   /app/data                       — SQLite DB (papers.db)
#   /app/.cache                     — Codex/crawler logs
#   /app/crawler/sites/custom       — Codex-generated crawlers
#   /app/crawler/sites/configs      — per-site JSON configs
VOLUME ["/app/data", "/app/.cache", "/app/crawler/sites/custom", "/app/crawler/sites/configs"]

CMD ["/usr/bin/supervisord", "-n", "-c", "/etc/supervisor/supervisord.conf"]
