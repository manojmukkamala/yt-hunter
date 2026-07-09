FROM ghcr.io/astral-sh/uv:0.11.28-python3.13-trixie-slim

WORKDIR /app

# Install dependencies from the lock file (project not installed yet).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

# Copy source code and install the project itself.
COPY . .
RUN uv sync --frozen

ENV PATH="/app/.venv/bin:$PATH"

ENTRYPOINT ["python", "main.py"]
