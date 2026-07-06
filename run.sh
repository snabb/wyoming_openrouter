#!/bin/sh
# Run script for Wyoming OpenRouter add-on
set -e

CONFIG_PATH=/data/options.json

# `models`/`languages` are lists; read them as comma-separated strings with jq.
read_list() {
    jq -r --arg k "$1" 'if (.[$k] | type) == "array" then (.[$k] | join(","))
           elif (.[$k] | type) == "string" then .[$k]
           else "" end' "$CONFIG_PATH" 2>/dev/null
}

# Home Assistant Supervisor writes /data/options.json for every app regardless
# of whether bashio is available -- and it never is here, since this project
# doesn't build from an HA base image. Read it directly with jq; fall back to
# plain env vars for standalone Docker.
if [ -f "$CONFIG_PATH" ]; then
    API_KEY=$(jq -r '.api_key // ""' "$CONFIG_PATH")
    MODELS=$(read_list models)
    LANGUAGES=$(read_list languages)
    DEFAULT_LANGUAGE=$(jq -r '.default_language // ""' "$CONFIG_PATH")
    TIMEOUT=$(jq -r '.timeout // 60' "$CONFIG_PATH")
    DEBUG=$(jq -r '.debug // false' "$CONFIG_PATH")
else
    # Defaults for standalone usage (also overridable via plain env vars,
    # e.g. from docker-compose.yml)
    API_KEY="${API_KEY:-${OPENROUTER_API_KEY:-}}"
    MODELS="${MODELS:-openai/gpt-4o-mini-transcribe}"
    LANGUAGES="${LANGUAGES:-en}"
    DEFAULT_LANGUAGE="${DEFAULT_LANGUAGE:-}"
    TIMEOUT="${TIMEOUT:-60}"
    DEBUG="${DEBUG:-false}"
fi

[ "$MODELS" = "null" ] && MODELS="openai/gpt-4o-mini-transcribe"
[ "$LANGUAGES" = "null" ] && LANGUAGES="en"
[ "$DEFAULT_LANGUAGE" = "null" ] && DEFAULT_LANGUAGE=""

if [ -z "$API_KEY" ]; then
    echo "ERROR: no OpenRouter API key configured (set the 'api_key' option or OPENROUTER_API_KEY)" >&2
    exit 1
fi

# POSIX sh has no arrays -- positional parameters via `set --` instead, so
# this script runs under any /bin/sh (busybox ash on Alpine, dash/bash
# elsewhere) without requiring bash to be installed just for this.
set -- \
    --host "0.0.0.0" \
    --port "10300" \
    --api-key "$API_KEY" \
    --models "$MODELS" \
    --languages "$LANGUAGES" \
    --timeout "$TIMEOUT"

if [ -n "$DEFAULT_LANGUAGE" ]; then
    set -- "$@" --default-language "$DEFAULT_LANGUAGE"
fi

if [ "$DEBUG" = "true" ]; then
    set -- "$@" --debug
fi

echo "========================================"
echo "Wyoming OpenRouter Server"
echo "========================================"
echo "Models: $MODELS"
echo "Languages: $LANGUAGES (default hint: ${DEFAULT_LANGUAGE:-<auto-detect>})"
echo "Timeout: ${TIMEOUT}s"
echo "Debug: $DEBUG"
echo "========================================"

# Function to send discovery info to Home Assistant
send_discovery() {
    # Wait for the server to be ready
    local max_wait=60
    local waited=0
    echo "Waiting for Wyoming server to be ready for discovery..."

    while [ $waited -lt $max_wait ]; do
        if echo '{"type":"describe"}' | nc -w 2 localhost 10300 2>/dev/null | grep -q "openrouter"; then
            echo "Server is ready after ${waited}s"
            break
        fi
        sleep 2
        waited=$((waited + 2))
    done

    if [ $waited -ge $max_wait ]; then
        echo "Warning: Timed out waiting for server to start for discovery"
        return 1
    fi

    # Small delay to ensure server is fully ready
    sleep 1

    # Check if running in Home Assistant (supervisor API available)
    if [ -n "$SUPERVISOR_TOKEN" ]; then
        local hostname discovery_host ipv4
        # Get hostname and convert underscores to hyphens for valid DNS name
        hostname=$(hostname | tr '_' '-')

        # Prefer advertising our IPv4 address over the hostname. The hassio
        # network is dual-stack, so the add-on hostname resolves to BOTH an
        # IPv4 and an IPv6 (ULA) address. Home Assistant Core resolves the
        # IPv6 first; on hosts with IPv6 disabled (the common case) that
        # address is unroutable, so Core's connection attempt hangs on a
        # dropped SYN and times out ("Unable to connect" -> the STT entity
        # stays stuck "Initialising"). Advertising the IPv4 address sidesteps
        # the broken IPv6 path entirely. Fall back to the hostname if we
        # cannot determine an IPv4 address.
        ipv4=$(hostname -i 2>/dev/null | tr ' ' '\n' \
            | grep -E '^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)' | head -n1)
        if [ -z "$ipv4" ]; then
            ipv4=$(getent ahostsv4 "$hostname" 2>/dev/null | awk '{print $1; exit}')
        fi
        if [ -n "$ipv4" ]; then
            discovery_host="$ipv4"
            echo "Advertising IPv4 address ${ipv4} for discovery (avoids unreachable IPv6 on IPv6-disabled hosts)"
        else
            discovery_host="$hostname"
            echo "Could not determine IPv4 address; falling back to hostname ${hostname} for discovery"
        fi
        echo "Sending discovery for host: ${discovery_host}:10300"

        # Retry discovery up to 3 times
        local retry=0
        local max_retries=3
        while [ $retry -lt $max_retries ]; do
            local response
            response=$(curl -s -X POST \
                -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" \
                -H "Content-Type: application/json" \
                -d "{\"service\": \"wyoming\", \"config\": {\"uri\": \"tcp://${discovery_host}:10300\"}}" \
                "http://supervisor/discovery" 2>&1)

            if echo "$response" | grep -q '"result".*"ok"'; then
                echo "Successfully sent discovery information to Home Assistant"
                return 0
            else
                echo "Discovery attempt $((retry + 1)) response: $response"
                retry=$((retry + 1))
                sleep 2
            fi
        done
        echo "Warning: Failed to send discovery after ${max_retries} attempts"
    else
        echo "Not running in Home Assistant (no SUPERVISOR_TOKEN) - skipping discovery"
    fi
}

# Start discovery in background (will wait for server to be ready)
send_discovery &

# Run the server (packages installed to system Python)
exec python3 -m wyoming_openrouter "$@"
