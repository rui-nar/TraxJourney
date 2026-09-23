# Flutter test environment for TraxJourney.
#
# Tests run here rather than on Windows: Smart App Control blocks the SDK's
# unsigned impellerc.exe, so a native `flutter test` dies before the first test.
#
# The SDK is pinned to a git tag, and the image name encodes it, so a mismatch
# with the dev box is visible rather than silent. Rebuild whenever Flutter is
# upgraded — a container on 3.47.1 proves nothing about a box that has moved on.
# Deliberately NOT ghcr.io/cirruslabs/flutter:stable: it lagged real stable by
# three months and publishes no exact version tags.
FROM debian:bookworm-slim

ARG FLUTTER_TAG=3.47.1

# rsync is baked in on purpose. It used to be apt-get'ed into the running
# container, so it vanished with every rebuild and the sync command failed in a
# way that looked like a test failure.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git unzip xz-utils zip libglu1-mesa rsync \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 --branch "${FLUTTER_TAG}" \
        https://github.com/flutter/flutter.git /opt/flutter \
    && git config --global --add safe.directory /opt/flutter

ENV PATH="/opt/flutter/bin:/opt/flutter/bin/cache/dart-sdk/bin:${PATH}"
ENV PUB_CACHE=/root/.pub-cache

# Warm the toolchain into the image so the first run is not a download.
RUN flutter config --no-analytics >/dev/null 2>&1 || true; \
    flutter --version && flutter precache --universal

# /src is the read-only bind mount of the Windows checkout; /work is the Linux
# working copy that keeps .dart_tool, build/ and the pub cache warm between
# runs. Syncing /src -> /work is what the test command does.
WORKDIR /work
CMD ["sleep", "infinity"]
