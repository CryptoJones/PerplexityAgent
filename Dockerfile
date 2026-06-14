# Copyright 2026 Aaron K. Clark. All rights reserved.
#
# Runtime image for the PerplexityAgent MCP server. Runs as a non-root user
# over stdio. In production, pin the base by digest:
#   FROM python:3.12-slim@sha256:<digest>
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install deps first (cached unless the manifest changes). README.md is copied
# because pyproject declares `readme = "README.md"` and hatchling needs it to
# build the wheel — a real, easy-to-miss build break.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --upgrade pip && pip install .

# Drop to an unprivileged user; give it a home for any state.
RUN useradd --create-home --uid 1000 app && chown -R app:app /app
USER app

# MCP talks JSON-RPC over stdio. Don't expose a port by default.
ENTRYPOINT ["perplexity-agent"]
