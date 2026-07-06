# Wyoming OpenRouter

[Wyoming protocol](https://github.com/OHF-Voice/wyoming) server for
[OpenRouter](https://openrouter.ai/)'s speech-to-text (STT) APIs, for use as a
[Home Assistant](https://www.home-assistant.io/) speech-to-text provider.

Modeled after [wyoming_bluetts](https://github.com/snabb/wyoming_bluetts),
which this project follows for its overall structure (pip package + Home
Assistant app packaging, event handler design).

## Features

- **Any OpenRouter STT model**: `openai/gpt-4o-mini-transcribe`,
  `openai/whisper-1`, `mistralai/voxtral-mini-transcribe`,
  `qwen/qwen3-asr-flash-...`, and others -- configurable, no code changes
  needed for new models. At startup the server fetches and logs the live
  OpenRouter STT model catalog with current prices.
- **Cost and latency logged per request** (`Transcribe: model=... language=...
  audio=...s latency=...ms cost=$...`).
- **Home Assistant sensor entities** for request count, cumulative cost, and
  last/average latency, pushed automatically when running as the Home
  Assistant App (via the Supervisor's Core API proxy -- no MQTT broker or
  separate integration needed).
- No local inference, no GPU, no model downloads: a thin async HTTP client.
- Ships both as a pip-installable Python package and a Home Assistant app.

## How it works

OpenRouter's transcription API (`POST /api/v1/audio/transcriptions`) takes one
complete audio clip per request and returns one complete transcript -- there
is no streaming transcription endpoint to use. Home Assistant's own Wyoming
integration doesn't consume streamed transcript output either (it always
waits for a single final `Transcript` event), so this server simply
accumulates the audio Home Assistant sends for one utterance, sends it to
OpenRouter in one request once the utterance ends, and returns the transcript.

## Quick start

### Home Assistant app

Settings → Apps → Install app → ⋮ (three dots) → Repositories → Add this
repository's URL, then install "Wyoming OpenRouter" from the store. Set your
OpenRouter API key in the app's Configuration tab. It auto-discovers into
Home Assistant via the Wyoming protocol.

### Standalone (uv)

```bash
git clone https://github.com/snabb/wyoming_openrouter.git
uv tool install ./wyoming_openrouter
OPENROUTER_API_KEY=sk-or-v1-... wyoming-openrouter --debug
```

### Docker

```bash
docker run --rm -p 10300:10300 \
  -e OPENROUTER_API_KEY=sk-or-v1-... \
  ghcr.io/snabb/wyoming_openrouter:latest
```

See [docker-compose.yml](docker-compose.yml) for a persistent deployment
example, including a comment showing how to bind to a specific interface
(e.g. a WireGuard IP) instead of all interfaces, if Home Assistant reaches
this host over a VPN. Add it in Home Assistant via Settings → Devices &
Services → Add integration → "Wyoming Protocol".

## Configuration (CLI flags)

| Flag | Default | Description |
|---|---|---|
| `--host` | `0.0.0.0` | Host to bind to |
| `--port` | `10300` | Port to bind to |
| `--api-key` | `$OPENROUTER_API_KEY` | OpenRouter API key |
| `--models` | `openai/gpt-4o-mini-transcribe` | Comma-separated OpenRouter STT model slugs to advertise; the first is the default used for every request from Home Assistant |
| `--languages` | `en` | Comma-separated languages to advertise to Home Assistant's UI |
| `--default-language` | *(unset)* | Language hint sent to OpenRouter when a request doesn't specify one; unset lets the model auto-detect |
| `--timeout` | `60` | HTTP timeout (seconds) for OpenRouter requests |
| `--debug` | off | Verbose logging |

## Models

Any model slug from OpenRouter's
[STT model catalog](https://openrouter.ai/models?output_modalities=transcription)
works. Check this server's startup log for the current live catalog and
prices, or query it directly:

```bash
curl -s 'https://openrouter.ai/api/v1/models?output_modalities=transcription' | jq '.data[] | {id, pricing}'
```

`--models` accepts a comma-separated list; a Wyoming client that sets
`Transcribe.name` to one of the configured slugs gets that model for that
request. Home Assistant's own Wyoming integration never sets this field, so
from Home Assistant only the first (default) configured model is ever
actually used -- the list is mainly useful for switching the default model,
or for other Wyoming clients that do pick a model by name.

## Metrics in Home Assistant

When running as the Home Assistant App, four sensor entities are pushed
automatically after every request:

- `sensor.wyoming_openrouter_stt_request_count`
- `sensor.wyoming_openrouter_stt_total_cost`
- `sensor.wyoming_openrouter_stt_last_latency_ms`
- `sensor.wyoming_openrouter_stt_avg_latency_ms`

This uses Home Assistant's own Core API through the Supervisor proxy (the
app's `homeassistant_api: true` manifest option), not MQTT. In standalone
Docker there's no Supervisor to push to, so these are skipped -- request
count/cost/latency are still visible in the container's log lines.

## Limitations

- No streaming transcription: OpenRouter's STT endpoint takes one full audio
  clip per request, and Home Assistant's Wyoming integration doesn't consume
  streamed transcript output either, so each utterance is one request.
- Home Assistant only ever uses the first configured model; per-request model
  selection only works with other Wyoming clients that set `Transcribe.name`.
- Metrics sensor entities are plain Home Assistant states pushed via the Core
  API, not full registry-managed entities tied to a device/config entry.

## Development

```bash
uv sync --all-extras --dev
prek run --all-files
uv run pytest
```

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT. Uses the [Wyoming protocol](https://github.com/OHF-Voice/wyoming) (MIT).
