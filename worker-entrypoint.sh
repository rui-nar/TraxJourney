#!/bin/sh
# Worker container entry point (issue #173).
#
# Deliberately does NOT run `alembic upgrade head`: the API container owns the
# schema. Two containers racing migrations at boot is the failure this avoids.
set -e
export TRAXJOURNEY_ROLE=worker
exec python -m src.jobs.worker "$@"
