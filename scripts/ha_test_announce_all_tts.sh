#!/usr/bin/env bash
# Play a short test announcement through every configured Wyoming
# OpenRouter TTS entity, one at a time, on a given media player -- lets you
# listen to every configured voice back to back after a
# ha_app_configure_all_models.sh run.
#
# Requires: HASS_SERVER, HASS_TOKEN, hass-cli, curl, jq. There's no default media
# player -- pass one explicitly.
#
# Usage: scripts/ha_test_announce_all_tts.sh media_player.living_room_speaker [en|fi|de|fr|es]
set -euo pipefail

: "${HASS_SERVER:?HASS_SERVER must be set}"
: "${HASS_TOKEN:?HASS_TOKEN must be set}"

media_player="${1:?Usage: $0 <media_player.entity_id> [en|fi|de|fr|es]}"
language="${2:-en}"
case "$language" in
    en) test_message="This is a test announcement." ;;
    fi) test_message="Tämä on testi-ilmoitus." ;;
    de) test_message="Dies ist eine Testansage." ;;
    fr) test_message="Ceci est une annonce de test." ;;
    es) test_message="Este es un anuncio de prueba." ;;
    *)
        echo "Unsupported language '$language'; choose en, fi, de, fr, or es." >&2
        exit 2
        ;;
esac
# Safety cap per announcement in case a media player's state never settles.
MAX_WAIT_SECONDS="${MAX_WAIT_SECONDS:-30}"

engine_json=$(hass-cli -o json raw ws tts/engine/list)
all_entities=$(echo "$engine_json" | jq -r \
    '.result.providers[]
     | select(.engine_id | startswith("tts.openrouter"))
     | .engine_id' | sort)
entities=$(echo "$engine_json" | jq -r --arg language "$language" \
    '.result.providers[]
     | select(.engine_id | startswith("tts.openrouter"))
     | select(.supported_languages | index($language))
     | .engine_id' | sort)

if [ -z "$all_entities" ]; then
    echo "No tts.openrouter_* entities found. Run ha_app_configure_all_models.sh first." >&2
    exit 1
fi
if [ -z "$entities" ]; then
    echo "No tts.openrouter_* entities advertise language '$language'." >&2
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
    name=$(echo "${entity#tts.openrouter_}" | tr '_' ' ')
    message="$name. $test_message"
    echo "Announcing via $entity: \"$message\""
    curl -sS -X POST -H "Authorization: Bearer $HASS_TOKEN" \
        -H "Content-Type: application/json" \
        "$HASS_SERVER/api/services/tts/speak" \
        -d "$(jq -n --arg eid "$entity" --arg mp "$media_player" --arg msg "$message" \
            --arg language "$language" \
            '{entity_id: $eid, media_player_entity_id: $mp, message: $msg, language: $language}')" \
        > /dev/null
    wait_until_idle
    count=$((count + 1))
done

skipped=$(comm -23 <(printf '%s\n' "$all_entities") <(printf '%s\n' "$entities") | sed '/^$/d' | wc -l)
echo "Done -- announced in $language through $count TTS engines on $media_player; skipped $skipped."
