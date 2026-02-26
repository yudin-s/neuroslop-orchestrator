FROM python:3.11-slim

# Minimal OS deps: git for patch apply, curl for quick checks
RUN apt-get update \
  && apt-get install -y --no-install-recommends git ca-certificates curl nodejs npm \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY local_agents /app/local_agents
COPY pyproject.toml /app/pyproject.toml

# Default entrypoint: `python -m local_agents ...`
ENTRYPOINT ["python", "-m", "local_agents"]
