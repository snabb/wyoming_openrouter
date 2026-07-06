#!/bin/sh
# Run script for Wyoming OpenRouter add-on
set -e

CONFIG_PATH=/data/options.json
if [ ! -f "$CONFIG_PATH" ]; then
    # Standalone Docker: point at your own tasks JSON file (same shape as
    # Supervisor's options.json -- see README.md) via this env var.
    CONFIG_PATH="${CONFIG_PATH_OVERRIDE:-/config/tasks.json}"
fi

if [ ! -f "$CONFIG_PATH" ]; then
    echo "ERROR: no config file found at $CONFIG_PATH. Supervisor should have" \
        "written /data/options.json, or set CONFIG_PATH_OVERRIDE to point at" \
        "your own tasks JSON file." >&2
    exit 1
fi

DEBUG=$(jq -r '.debug // false' "$CONFIG_PATH" 2>/dev/null || echo false)

echo "========================================"
echo "Wyoming OpenRouter Server"
echo "========================================"
echo "Config: $CONFIG_PATH"
jq -r '.tasks[] | "  - \(.name) (\(.type)) port=\(.port) model=\(.model)"' "$CONFIG_PATH" 2>/dev/null
echo "Debug: $DEBUG"
echo "========================================"

# Sends one Wyoming-discovery message per configured task/port. This only
# pre-fills host:port when the user adds each "Wyoming Protocol" integration
# entry in Home Assistant -- it does not add the entry itself; each task is
# still added individually, same as before, just with less manual typing.
send_discovery() {
    if [ -z "$SUPERVISOR_TOKEN" ]; then
        echo "Not running in Home Assistant (no SUPERVISOR_TOKEN) - skipping discovery"
        return 0
    fi

    hostname_value=$(hostname | tr '_' '-')
    # Prefer advertising our IPv4 address over the hostname. The hassio
    # network is dual-stack, so the add-on hostname resolves to BOTH an
    # IPv4 and an IPv6 (ULA) address. Home Assistant Core resolves the IPv6
    # first; on hosts with IPv6 disabled (the common case) that address is
    # unroutable, so Core's connection attempt hangs on a dropped SYN and
    # times out. Advertising the IPv4 address sidesteps that entirely.
    ipv4=$(hostname -i 2>/dev/null | tr ' ' '\n' \
        | grep -E '^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)' | head -n1)
    if [ -z "$ipv4" ]; then
        ipv4=$(getent ahostsv4 "$hostname_value" 2>/dev/null | awk '{print $1; exit}')
    fi
    if [ -n "$ipv4" ]; then
        discovery_host="$ipv4"
    else
        discovery_host="$hostname_value"
    fi

    jq -r '.tasks[] | "\(.name) \(.port)"' "$CONFIG_PATH" | while read -r name port; do
        max_wait=60
        waited=0
        while [ "$waited" -lt "$max_wait" ]; do
            if echo '{"type":"describe"}' | nc -w 2 localhost "$port" 2>/dev/null | grep -q "openrouter"; then
                break
            fi
            sleep 2
            waited=$((waited + 2))
        done
        if [ "$waited" -ge "$max_wait" ]; then
            echo "Warning: timed out waiting for task '$name' on port $port to start"
            continue
        fi

        retry=0
        max_retries=3
        sent=false
        while [ "$retry" -lt "$max_retries" ]; do
            response=$(curl -s -X POST \
                -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" \
                -H "Content-Type: application/json" \
                -d "{\"service\": \"wyoming\", \"config\": {\"uri\": \"tcp://${discovery_host}:${port}\"}}" \
                "http://supervisor/discovery" 2>&1)
            if echo "$response" | grep -q '"result".*"ok"'; then
                echo "Sent discovery for task '$name' (${discovery_host}:${port})"
                sent=true
                break
            fi
            retry=$((retry + 1))
            sleep 2
        done
        if [ "$sent" != "true" ]; then
            echo "Warning: failed to send discovery for task '$name' after ${max_retries} attempts"
        fi
    done
}

send_discovery &

set --
if [ "$DEBUG" = "true" ]; then
    set -- --debug
fi

# Run the server (packages installed to system Python)
exec python3 -m wyoming_openrouter --config "$CONFIG_PATH" "$@"
