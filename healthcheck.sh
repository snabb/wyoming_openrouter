#!/bin/sh
# Docker HEALTHCHECK: probe every configured task's Wyoming port, since the
# set of ports is dynamic (config-driven) rather than a single fixed port.
set -e

CONFIG_PATH=/data/options.json
if [ ! -f "$CONFIG_PATH" ]; then
    CONFIG_PATH="${CONFIG_PATH_OVERRIDE:-/config/tasks.json}"
fi

if [ ! -f "$CONFIG_PATH" ]; then
    echo "healthcheck: no config file found at $CONFIG_PATH" >&2
    exit 1
fi

ports=$(jq -r '.tasks[].port' "$CONFIG_PATH" 2>/dev/null)
if [ -z "$ports" ]; then
    echo "healthcheck: no task ports found in $CONFIG_PATH" >&2
    exit 1
fi

for port in $ports; do
    if ! echo '{"type":"describe"}' | nc -w 5 localhost "$port" 2>/dev/null | grep -q "openrouter"; then
        echo "healthcheck: task on port $port did not respond" >&2
        exit 1
    fi
done

exit 0
