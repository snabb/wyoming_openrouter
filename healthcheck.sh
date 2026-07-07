#!/bin/sh
# Docker HEALTHCHECK: probe one configured task's Wyoming port.
#
# Checking just one port (not every configured task) is deliberate, not a
# shortcut: every task's server runs as a member of the same asyncio
# TaskGroup in one process (see __main__.py), which binds all ports
# fail-fast -- if any single task's port failed to bind, the whole process
# would have crashed already and wouldn't be running at all. So the process
# being alive and answering on any one port already proves every other
# port is bound and the event loop is responsive too; probing all of them
# only adds time for no extra guarantee (confirmed live: a sequential
# all-ports version blew past the HEALTHCHECK's own --timeout with enough
# tasks configured and permanently marked a perfectly working container
# "unhealthy").
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

if ! echo '{"type":"describe"}' | nc -w 2 localhost "$port" 2>/dev/null | grep -q "openrouter"; then
    echo "healthcheck: task on port $port did not respond" >&2
    exit 1
fi

exit 0
