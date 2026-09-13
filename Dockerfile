FROM python:3.14-slim

# The source label is what links a package pushed to ghcr.io back to its
# repository; docker-build.yml passes the same one to the build.
LABEL org.opencontainers.image.source="https://github.com/rui-nar/TraxJourney" \
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

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# Run migrations then start the server
COPY entrypoint.sh /entrypoint.sh
# One image, two roles (issue #173): the default CMD serves the API; the worker
# service overrides it with /worker-entrypoint.sh. Same code, same deploy.
COPY worker-entrypoint.sh /worker-entrypoint.sh
RUN chmod +x /entrypoint.sh /worker-entrypoint.sh
CMD ["/entrypoint.sh"]
