#!/usr/bin/env bash
# Install the freshest published version of the Wyoming OpenRouter Home
# Assistant app from its app repository.
#
# The repository (https://github.com/snabb/wyoming_openrouter) must already
# be added in Settings > Apps > App store > ... > Repositories -- this
# script only reloads and installs from it, it doesn't add the repository
# itself.
#
# Requires: HASS_SERVER, HASS_TOKEN, hass-cli, jq.
#
# Usage: scripts/ha_app_install.sh
set -euo pipefail

: "${HASS_SERVER:?HASS_SERVER must be set (e.g. http://homeassistant.local:8123)}"
: "${HASS_TOKEN:?HASS_TOKEN must be set (a Home Assistant long-lived access token)}"

APP_NAME="Wyoming OpenRouter"

# The newer /apps and /store/apps REST paths error out ("unknown_error") on
# this Supervisor version -- only the legacy /addons and /store/addons
# paths (still fully supported, just an older name) actually work.
# Everything user-facing below still says "app".
echo "Reloading the Supervisor app store (picks up the latest repository commit)..."
hass-cli -o json raw ws supervisor/api \
    --json '{"endpoint":"/store/reload","method":"post","timeout":null}' > /dev/null

slug=$(hass-cli -o json raw ws supervisor/api \
    --json '{"endpoint":"/store/addons","method":"get"}' \
    | jq -r --arg name "$APP_NAME" '.result.addons[] | select(.name == $name) | .slug' | head -n1)

if [ -z "$slug" ]; then
    echo "ERROR: '$APP_NAME' was not found in the app store. Add its" \
        "repository first (Settings > Apps > App store > ... > Repositories)." >&2
    exit 1
fi

echo "Installing '$APP_NAME' (slug: $slug)..."
hass-cli -o json raw ws supervisor/api \
    --json "{\"endpoint\":\"/store/addons/$slug/install\",\"method\":\"post\",\"timeout\":null}"

echo "Waiting for the app to start (boot: auto should start it automatically after install)..."
state=""
for _ in $(seq 1 30); do
    state=$(hass-cli -o json raw ws supervisor/api \
        --json "{\"endpoint\":\"/addons/$slug/info\",\"method\":\"get\"}" \
        | jq -r '.result.state')
    if [ "$state" = "started" ]; then
        echo "Installed and running (slug: $slug)."
        exit 0
    fi
    sleep 2
done

echo "WARNING: app did not reach 'started' state within 60s (last state: $state)." \
    "Check the app's Log tab." >&2
exit 1
