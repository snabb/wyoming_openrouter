#!/usr/bin/env bash
# Uninstall the Wyoming OpenRouter Home Assistant app, if currently installed.
#
# All Supervisor management operations (install/uninstall/options/restart)
# require the websocket "supervisor/api" command -- Home Assistant Core's
# plain REST proxy at /api/hassio/... only allows a narrow allowlist of
# paths (logs, backups, changelog/documentation), so hass-cli's websocket
# support is required here, not curl alone.
#
# Requires: HASS_SERVER, HASS_TOKEN (a Home Assistant long-lived access
# token for an admin user), hass-cli, jq.
#
# Usage: scripts/ha_app_uninstall.sh
set -euo pipefail

: "${HASS_SERVER:?HASS_SERVER must be set (e.g. http://homeassistant.local:8123)}"
: "${HASS_TOKEN:?HASS_TOKEN must be set (a Home Assistant long-lived access token)}"

APP_NAME="Wyoming OpenRouter"

# The newer /apps REST paths error out ("unknown_error") on this Supervisor
# version -- only the legacy /addons paths (still fully supported, just an
# older name) actually work. Everything user-facing below still says "app".
slug=$(hass-cli -o json raw ws supervisor/api \
    --json '{"endpoint":"/addons","method":"get"}' \
    | jq -r --arg name "$APP_NAME" '.result.addons[] | select(.name == $name) | .slug' | head -n1)

if [ -z "$slug" ]; then
    echo "'$APP_NAME' is not currently installed -- nothing to do."
    exit 0
fi

echo "Uninstalling '$APP_NAME' (slug: $slug)..."
hass-cli -o json raw ws supervisor/api \
    --json "{\"endpoint\":\"/addons/$slug/uninstall\",\"method\":\"post\",\"timeout\":null}"
echo "Done."
