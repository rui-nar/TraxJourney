# Flutter test environment

Flutter tests for this project run in a container, not on Windows directly:
Smart App Control blocks the SDK's unsigned `impellerc.exe`, so a native
`flutter test` dies before the first test.

This directory is the build for that container. It exists because the
environment had to be reconstructed from notes twice — a note is not a build.

## Build and create

The image name encodes the SDK version so a mismatch with your machine is
visible rather than silent. Check yours first and use it throughout:

```bash
flutter --version          # must match FLUTTER_TAG below

docker build -t flutter-3471 --build-arg FLUTTER_TAG=3.47.1 tools/flutter-test

docker run -d --name traxjourney-flutter-3.47.1-tests \
  --restart unless-stopped \
  -v "E:/Dev/TraxJourney/flutter_client:/src:ro" \
  flutter-3471
```

`--restart unless-stopped`, and **not** `--rm`: the container keeps a warm
`/work` holding the Linux `.dart_tool`, `build/` and pub cache, which is the
whole point. A cold run re-resolves packages and re-downloads artifacts.

## Run the tests

```bash
docker exec traxjourney-flutter-3.47.1-tests bash -c "rsync -a --delete \
  --exclude '.dart_tool' --exclude 'build' --exclude '.flutter-plugins*' \
  /src/ /work/ && cd /work && flutter test"
```

The excludes matter: `/src` is the **Windows** checkout, and copying its
`.dart_tool/` and `build/` into `/work` poisons the Linux toolchain.

`flutter analyze` is not affected by Smart App Control and can still be run
natively from `flutter_client/`.

## Rebuild whenever Flutter is upgraded

A container on 3.47.1 proves nothing about a machine that has moved on. Bump
`FLUTTER_TAG`, rebuild, and rename the image and container to match.

## Two things this build fixes

- **`rsync` is baked in.** It used to be `apt-get`-ed into the running
  container, so it vanished with every rebuild — and since the command above
  depends on it, its absence surfaced as a failing sync rather than a missing
  tool.
- **The toolchain is precached into the image**, so the first run is a test run
  rather than a download.

## Why not `ghcr.io/cirruslabs/flutter:stable`

It lagged real stable by three months (3.44.0 while this machine ran 3.47.1)
and publishes no exact version tags, which is why the SDK is cloned from its
git tag instead.
