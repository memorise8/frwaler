#!/bin/sh
set -eu

node scripts/verify-runtime-mounts.mjs
exec "$@"
