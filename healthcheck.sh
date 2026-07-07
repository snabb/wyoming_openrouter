#!/bin/sh
# Docker HEALTHCHECK: probe one configured task's Wyoming port.
#
# Checking just one port (not every configured task) is deliberate, not a
# shortcut: every task's server runs as a member of the same asyncio
# TaskGroup in one process (see __main__.py), which binds all ports
# fail-fast -- if any single task's port failed to bind, the whole process
# would have crashed already and wouldn't be running at all. So the process
# being alive and answering on any one port already proves every other
# port is bound and the event loop is responsive too.
#
# Uses wyoming_openrouter.probe rather than `nc`: `nc -w N` doesn't exit as
# soon as it receives the expected response, only when the connection
# closes or its own idle timeout elapses -- and Wyoming connections are
# intentionally kept open by the server, so every `nc` probe paid the full
# idle timeout as a fixed floor regardless of how fast the real answer
# arrived. Confirmed live via `docker inspect`: with enough tasks, a
# sequential all-ports `nc`-based version blew past this HEALTHCHECK's own
# --timeout and permanently marked a perfectly working container
# "unhealthy".
set -e

CONFIG_PATH=/data/options.json
if [ ! -f "$CONFIG_PATH" ]; then
    CONFIG_PATH="${CONFIG_PATH_OVERRIDE:-/config/tasks.json}"
fi

if [ ! -f "$CONFIG_PATH" ]; then
    echo "healthcheck: no config file found at $CONFIG_PATH" >&2
    exit 1
fi

port=$(jq -r '.tasks[0].port' "$CONFIG_PATH" 2>/dev/null)
if [ -z "$port" ] || [ "$port" = "null" ]; then
    echo "healthcheck: no task ports found in $CONFIG_PATH" >&2
    exit 1
fi

exec python3 -m wyoming_openrouter.probe "$port"
