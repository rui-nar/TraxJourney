#!/bin/sh
set -e
alembic upgrade head
# --timeout-keep-alive must exceed Caddy's upstream idle timeout (2m by default,
# docs/DEPLOYMENT_VPS.md section 2). uvicorn's default of 5 s makes it the side
# that closes a pooled connection first, and when Caddy reuses that connection at
# the same instant the request fails with a 502 ("EOF" / "connection reset by
# peer" in Caddy's log). GETs are retried transparently by Go's HTTP client;
# POST/PUT/DELETE are not. Issue #400 reproduces it and proves this value.
#
# --no-access-log: uvicorn's own access line printed the full client IP and the
# raw request line, query string included (issue #443). The app's middleware
# (api/middleware.py) logs each request instead: route template, status,
# duration and the client address truncated to its /24 (IPv6: /48).
#
# --forwarded-allow-ips: which peers' X-Forwarded-For that address is taken
# from. Inside the container the reverse proxy's connection arrives from the
# Docker network's gateway (it comes in through the published port), not from
# 127.0.0.1, so uvicorn's default trusts nothing and the log would show the
# gateway's /24 for every request. Set FORWARDED_ALLOW_IPS in .env — see
# .env.example. The shell fallback is deliberate: a blank .env line reaches
# uvicorn as an empty string, which it reads as "trust nobody", not as unset.
exec uvicorn api.router:app --host 0.0.0.0 --port 8000 --timeout-keep-alive 300 --no-access-log --proxy-headers --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-127.0.0.1}"
