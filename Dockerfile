# NYX service image.
#
# Safe by default: it binds loopback, so exposing it requires an explicit NYX_HOST *and* an
# explicit NYX_AUTH_MODE other than "none" -- nyx.config.Settings refuses the unsafe combination,
# so a misconfigured container fails to start rather than serving unauthenticated on a network
# interface.
#
# No secret is baked in. Every credential comes from the environment at run time.

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

WORKDIR /app

# uv first, so a change to the source does not invalidate the dependency layer.
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /uvx /bin/

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./

RUN uv sync --frozen --no-dev --no-install-project

# The application itself, then a non-root user to run it.
RUN uv sync --frozen --no-dev

RUN useradd --create-home --uid 10001 nyx
USER nyx

EXPOSE 8000

# Migrations run at container start, so a fresh volume is ready before the first request.
# The bind is loopback unless NYX_HOST says otherwise, and a non-loopback bind requires
# NYX_AUTH_MODE to be set explicitly.
CMD ["sh", "-c", "uv run alembic upgrade head && uv run uvicorn nyx.api.app:create_app --factory --host ${NYX_HOST:-127.0.0.1} --port ${NYX_PORT:-8000}"]
