# syntax=docker/dockerfile:1

# The bot's container image. Built and pushed by `pulumi up` (see infra/bot.py).
#
# Two stages: the first installs the locked dependencies and the bot into a
# virtual environment; the second copies only that environment, so the image
# that runs has no build tools and no source tree.

FROM python:3.13-slim AS build

# Same uv version that wrote uv.lock.
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first: this layer is rebuilt only when the lock file changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Then the bot itself, installed as a normal package (not a link to /app/src).
COPY README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable


FROM python:3.13-slim

RUN groupadd --system --gid 10001 traider \
 && useradd --system --uid 10001 --gid traider --no-create-home --shell /usr/sbin/nologin traider

COPY --from=build /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER 10001:10001
WORKDIR /app

ENTRYPOINT ["traider"]
CMD ["run"]
