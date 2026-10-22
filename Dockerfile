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

# Patch the base OS before installing anything. The published python:3.12-slim image is Debian
# trixie, and its shipped packages can lag the security updates. `uv sync` cannot help here --
# these are OS packages, not Python ones. The lists are removed in the same layer so they are not
# baked into the image.
#
# This clears whatever Debian HAS fixed since the image was published. It does not clear the
# eight HIGH CVEs listed in .trivyignore: Debian's security tracker marks trixie vulnerable with
# no fix for all of them (util-linux, systemd, ncurses, acl and perl are each marked <no-dsa>),
# and the fixes exist only in forky/sid. No forky-based Python image is published, so a future
# scan may still report them until the base moves.
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && rm -rf /var/lib/apt/lists/*

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
