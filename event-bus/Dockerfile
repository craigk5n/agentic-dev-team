FROM python:3.12-slim

# git for coding/review agents; semgrep and gitleaks for security scanning
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates make \
    && rm -rf /var/lib/apt/lists/*

# semgrep (pip is fastest install path in the image)
RUN pip install --no-cache-dir semgrep

# gitleaks binary
RUN curl -sSL https://github.com/gitleaks/gitleaks/releases/download/v8.18.4/gitleaks_8.18.4_linux_x64.tar.gz \
    | tar -xz -C /usr/local/bin gitleaks

WORKDIR /app

# Docker SDK for SANDBOX_MODE=docker (spawns ephemeral containers via the host socket)
RUN pip install --no-cache-dir docker>=7.0.0

# Install event-bus
COPY event-bus/pyproject.toml .
COPY event-bus/src/ src/
RUN pip install --no-cache-dir .

# Coding agent
COPY agents/coding/pyproject.toml /agents/coding/
COPY agents/coding/src/ /agents/coding/src/
RUN pip install --no-cache-dir /agents/coding

# Reviewer agent (Phase 4: code review, test runner, security scan)
COPY agents/reviewer/pyproject.toml /agents/reviewer/
COPY agents/reviewer/src/ /agents/reviewer/src/
RUN pip install --no-cache-dir /agents/reviewer

# Idea agent (Phase 5: generate proposals → SQLite work store)
COPY agents/idea/pyproject.toml /agents/idea/
COPY agents/idea/src/ /agents/idea/src/
RUN pip install --no-cache-dir /agents/idea

# Planner agent (Phase 5: decompose approved ideas → module + stories)
COPY agents/planner/pyproject.toml /agents/planner/
COPY agents/planner/src/ /agents/planner/src/
RUN pip install --no-cache-dir /agents/planner

# Temporal worker (Phase 6: durable PR review workflows)
# temporalio is optional — skip failures so the image still builds without it
COPY agents/temporal_worker/pyproject.toml /agents/temporal_worker/
COPY agents/temporal_worker/src/ /agents/temporal_worker/src/
RUN pip install --no-cache-dir temporalio>=1.4.0 && \
    pip install --no-cache-dir --no-deps /agents/temporal_worker || true

EXPOSE 8080

# Default: run the webhook receiver; override CMD for the worker
CMD ["uvicorn", "event_bus.main:app", "--host", "0.0.0.0", "--port", "8080", "--log-level", "info"]
