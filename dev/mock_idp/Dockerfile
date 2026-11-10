FROM ghcr.io/astral-sh/uv:0.11.33 AS uv

FROM python:3.14.7-slim-trixie

COPY --from=uv /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY dev ./dev

RUN useradd --system --uid 10002 --home-dir /app mock-idp

USER 10002:10002

EXPOSE 9001
CMD ["uvicorn", "dev.mock_idp.main:app", "--host", "0.0.0.0", "--port", "9001"]
