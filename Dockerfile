# uv, pinned to an exact version so every build installs with the same tool
# (#603). Dependabot's docker updates move this tag; the setup-uv `version:` in
# the CI workflows must move with it.
FROM ghcr.io/astral-sh/uv:0.13.6 AS uv

FROM python:3.14-slim

# The source label is what links a package pushed to ghcr.io back to its
# repository; docker-build.yml passes the same one to the build.
LABEL org.opencontainers.image.source="https://github.com/rui-nar/TraxJourney" \
      org.opencontainers.image.licenses="AGPL-3.0-only" \
      org.opencontainers.image.title="TraxJourney" \
      org.opencontainers.image.description="TraxJourney server: the API, the web client and the background workers"

# Build-time version (the git tag), exposed at runtime so /api/version can report
# it and the web client can detect a stale cached bundle. Defaults to "dev".
ARG APP_VERSION=dev
ENV APP_VERSION=$APP_VERSION

WORKDIR /app

# pyosmium's extension module links libexpat at runtime, and python:*-slim ships
# none of it: CPython statically links its own copy for `pyexpat`, so nothing
# provides libexpat.so.1 and `import osmium` fails with an ImportError. Only
# scripts/fetch_rail_data.py needs it — it builds the rail stores on the box
# from the published extracts (issue #345) — and it is the whole reason that
# step can run in this image at all, so do not drop this layer as unused.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libexpat1 \
 && rm -rf /var/lib/apt/lists/*

# ffmpeg encodes trip videos: the `video` worker pipes the frames it draws into
# a local ffmpeg process (libx264, MP4). The API and other workers never call it.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

# Python dependencies come from uv.lock (#603). --locked fails the build if the
# lock does not match pyproject.toml instead of resolving afresh, and
# --no-default-groups leaves the dev and release groups (pytest, anthropic) out.
# The venv is /app/.venv on the base image's own Python; putting its bin first
# on PATH keeps entrypoint.sh, worker-entrypoint.sh and
# `docker compose run --entrypoint python` working unchanged. The lock files are
# copied before the code so this layer is cached across code-only changes.
COPY --from=uv /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_NO_CACHE=1 \
    PATH="/app/.venv/bin:$PATH"
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-default-groups

COPY . .

EXPOSE 8000

# Run migrations then start the server
COPY entrypoint.sh /entrypoint.sh
# One image, two roles (issue #173): the default CMD serves the API; the worker
# service overrides it with /worker-entrypoint.sh. Same code, same deploy.
COPY worker-entrypoint.sh /worker-entrypoint.sh
RUN chmod +x /entrypoint.sh /worker-entrypoint.sh
CMD ["/entrypoint.sh"]
