#!/usr/bin/env bash
# Play a short test announcement through every configured Wyoming
# OpenRouter TTS entity, one at a time, on a given media player -- lets you
# listen to every configured voice back to back after a
# ha_app_configure_all_models.sh run.
#
# Requires: HASS_SERVER, HASS_TOKEN, curl, jq. There's no default media
# player -- pass one explicitly.
#
# Usage: scripts/ha_test_announce_all_tts.sh media_player.living_room_speaker
set -euo pipefail

: "${HASS_SERVER:?HASS_SERVER must be set}"
: "${HASS_TOKEN:?HASS_TOKEN must be set}"

media_player="${1:?Usage: $0 <media_player.entity_id>}"
# Safety cap per announcement in case a media player's state never settles.
MAX_WAIT_SECONDS="${MAX_WAIT_SECONDS:-30}"

entities=$(curl -sS -H "Authorization: Bearer $HASS_TOKEN" "$HASS_SERVER/api/states" \
    | jq -r '.[] | select(.entity_id | startswith("tts.openrouter")) | .entity_id' | sort)

if [ -z "$entities" ]; then
    echo "No tts.openrouter_* entities found. Run ha_app_configure_all_models.sh first." >&2
    exit 1
fi

wait_until_idle() {
    local waited=0
    # Give the media player a moment to leave "idle" before checking for it.
    sleep 2
    while [ "$waited" -lt "$MAX_WAIT_SECONDS" ]; do
        state=$(curl -sS -H "Authorization: Bearer $HASS_TOKEN" \
            "$HASS_SERVER/api/states/$media_player" | jq -r '.state')
        if [ "$state" != "playing" ]; then
            return 0
        fi
        sleep 2
        waited=$((waited + 2))
    done
}

count=0
for entity in $entities; do
    name=${entity#tts.openrouter_}
    message="This is a test of the $name voice."
    echo "Announcing via $entity: \"$message\""
    curl -sS -X POST -H "Authorization: Bearer $HASS_TOKEN" \
        -H "Content-Type: application/json" \
        "$HASS_SERVER/api/services/tts/speak" \
        -d "$(jq -n --arg eid "$entity" --arg mp "$media_player" --arg msg "$message" \
            '{entity_id: $eid, media_player_entity_id: $mp, message: $msg}')" \
        > /dev/null
    wait_until_idle
    count=$((count + 1))
done

echo "Done -- announced through $count TTS engines on $media_player."
