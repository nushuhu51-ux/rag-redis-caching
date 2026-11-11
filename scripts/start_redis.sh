#!/usr/bin/env bash
# Start a local, non-persistent Redis on $REDIS_PORT (default 6379) in the foreground.
exec redis-server --port "${REDIS_PORT:-6379}" --bind 127.0.0.1 --save "" --appendonly no
