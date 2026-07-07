#!/usr/bin/env bash
# Replace the Wyoming OpenRouter app's configuration with one task per live
# OpenRouter STT and TTS model (except SKIP_MODELS below), all using the
# same OPENROUTER_API_KEY, restart the app, and create (or refresh) a
# matching "Wyoming Protocol" integration entry in Home Assistant for every
# task -- this is the "add all engines" setup, not a small hand-picked one.
#
# Every TTS task uses audio_format=mp3 uniformly, following the same
# reasoning as scripts/model_matrix.py: not every model supports
# response_format=pcm, and there's no reliable way to know which from the
# catalog alone, so mp3 (decoded locally via mpg123, already in the image)
# is the one setting that works across the whole catalog. Each TTS task
# picks the first entry in the model's live supported_voices list -- a
# reasonable automatic default, not a curated "best" voice.
#
# Requires: HASS_SERVER, HASS_TOKEN, OPENROUTER_API_KEY, hass-cli, jq, curl,
# python3. The app must already be installed (see ha_app_install.sh).
#
# Usage: scripts/ha_app_configure_all_models.sh
set -euo pipefail

: "${HASS_SERVER:?HASS_SERVER must be set}"
: "${HASS_TOKEN:?HASS_TOKEN must be set}"
: "${OPENROUTER_API_KEY:?OPENROUTER_API_KEY must be set}"

APP_NAME="Wyoming OpenRouter"
# Known non-functional as of 2026-07 (returns empty audio via this
# endpoint) -- skip by default. Set SKIP_MODELS="" to include it anyway.
SKIP_MODELS="${SKIP_MODELS:-google/gemini-3.1-flash-tts-preview}"

# The newer /apps REST paths error out ("unknown_error") on this Supervisor
# version -- only the legacy /addons paths (still fully supported, just an
# older name) actually work. Everything user-facing below still says "app".
slug=$(hass-cli -o json raw ws supervisor/api \
    --json '{"endpoint":"/addons","method":"get"}' \
    | jq -r --arg name "$APP_NAME" '.result.addons[] | select(.name == $name) | .slug' | head -n1)

if [ -z "$slug" ]; then
    echo "ERROR: '$APP_NAME' is not installed. Run ha_app_install.sh first." >&2
    exit 1
fi

echo "Fetching live OpenRouter STT + TTS catalogs..."
stt_models=$(curl -sS 'https://openrouter.ai/api/v1/models?output_modalities=transcription' | jq -c '.data')
tts_models=$(curl -sS 'https://openrouter.ai/api/v1/models?output_modalities=speech' | jq -c '.data')

tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT
echo "$stt_models" > "$tmpdir/stt.json"
echo "$tts_models" > "$tmpdir/tts.json"

options_json=$(python3 - "$OPENROUTER_API_KEY" "$SKIP_MODELS" "$tmpdir/stt.json" "$tmpdir/tts.json" <<'PYEOF'
import json
import re
import sys

api_key, skip_csv, stt_path, tts_path = sys.argv[1:5]
skip = {m.strip() for m in skip_csv.split(",") if m.strip()}

BASE_PORT = 10300
MAX_PORT = 10319  # matches this app's reserved port range in config.yaml


def slug_name(model_id: str) -> str:
    return model_id.rsplit("/", 1)[-1]


def stt_task(model_id: str, port: int) -> dict:
    return {
        "name": slug_name(model_id),
        "api_key": api_key,
        "type": "stt",
        "port": port,
        "model": model_id,
        "timeout": 60,
        "language": "en",
        "default_language": "",
        "temperature": 0.0,
        "voice": "",
        "speed": 1.0,
        "audio_format": "pcm",
        "provider": "",
    }


def tts_task(model_id: str, port: int, voice: str) -> dict:
    return {
        "name": slug_name(model_id),
        "api_key": api_key,
        "type": "tts",
        "port": port,
        "model": model_id,
        "timeout": 60,
        "language": "en",
        "default_language": "",
        "temperature": 0.0,
        "voice": voice,
        "speed": 1.0,
        "audio_format": "mp3",
        "provider": "",
    }


stt_models = json.load(open(stt_path))
tts_models = json.load(open(tts_path))

tasks = []
port = BASE_PORT
for model in stt_models:
    if model["id"] in skip:
        continue
    tasks.append(stt_task(model["id"], port))
    port += 1

for model in tts_models:
    if model["id"] in skip:
        continue
    voices = model.get("supported_voices") or []
    if not voices:
        print(f"WARNING: {model['id']} has no supported_voices, skipping", file=sys.stderr)
        continue
    tasks.append(tts_task(model["id"], port, voices[0]))
    port += 1

if port - 1 > MAX_PORT:
    print(
        f"ERROR: {len(tasks)} tasks need ports {BASE_PORT}-{port - 1}, "
        f"which exceeds this app's reserved range (up to {MAX_PORT}). "
        "Narrow the catalog with SKIP_MODELS or widen the reserved range "
        "in config.yaml/Dockerfile first.",
        file=sys.stderr,
    )
    sys.exit(1)

print(json.dumps({"debug": True, "tasks": tasks}))
PYEOF
)

task_count=$(echo "$options_json" | jq '.tasks | length')
echo "Generated $task_count tasks (ports 10300-$((10300 + task_count - 1)))."

echo "Writing options to the app..."
hass-cli -o json raw ws supervisor/api --json "$(jq -n --arg slug "$slug" --argjson opts "$options_json" \
    '{endpoint: "/addons/\($slug)/options", method: "post", data: {options: $opts}}')" > /dev/null

echo "Restarting the app..."
hass-cli -o json raw ws supervisor/api \
    --json "{\"endpoint\":\"/addons/$slug/restart\",\"method\":\"post\",\"timeout\":null}" > /dev/null

echo "Waiting for the app to report 'started'..."
state=""
for _ in $(seq 1 30); do
    state=$(hass-cli -o json raw ws supervisor/api \
        --json "{\"endpoint\":\"/addons/$slug/info\",\"method\":\"get\"}" | jq -r '.result.state')
    [ "$state" = "started" ] && break
    sleep 2
done
if [ "$state" != "started" ]; then
    echo "ERROR: app did not reach 'started' state (last state: $state)." >&2
    exit 1
fi

ip=$(hass-cli -o json raw ws supervisor/api \
    --json "{\"endpoint\":\"/addons/$slug/info\",\"method\":\"get\"}" | jq -r '.result.ip_address')
echo "App is up at $ip. Waiting a bit for every task's port to come online..."
sleep 15

echo "Creating/refreshing Wyoming Protocol integration entries..."
existing_entries=$(curl -sS -H "Authorization: Bearer $HASS_TOKEN" \
    "$HASS_SERVER/api/config/config_entries/entry" | jq -c '[.[] | select(.domain == "wyoming")]')

echo "$options_json" | jq -c '.tasks[]' | while read -r task; do
    name=$(echo "$task" | jq -r '.name')
    port=$(echo "$task" | jq -r '.port')
    title="OpenRouter ($name)"

    entry_id=$(echo "$existing_entries" | jq -r --arg title "$title" \
        '.[] | select(.title == $title) | .entry_id' | head -n1)

    if [ -n "$entry_id" ]; then
        echo "  $title: reloading existing entry"
        curl -sS -X POST -H "Authorization: Bearer $HASS_TOKEN" \
            "$HASS_SERVER/api/config/config_entries/entry/$entry_id/reload" > /dev/null
    else
        echo "  $title: creating entry ($ip:$port)"
        flow_id=$(curl -sS -X POST -H "Authorization: Bearer $HASS_TOKEN" \
            -H "Content-Type: application/json" \
            "$HASS_SERVER/api/config/config_entries/flow" -d '{"handler":"wyoming"}' \
            | jq -r '.flow_id')
        curl -sS -X POST -H "Authorization: Bearer $HASS_TOKEN" \
            -H "Content-Type: application/json" \
            "$HASS_SERVER/api/config/config_entries/flow/$flow_id" \
            -d "$(jq -n --arg host "$ip" --argjson port "$port" '{host: $host, port: $port}')" \
            > /dev/null
    fi
done

echo "Done. Note: this doesn't remove integration entries for tasks that" \
    "existed before this run and aren't in the new list -- check for" \
    "leftovers manually if you changed SKIP_MODELS across runs."
