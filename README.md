# Wyoming OpenRouter STT and TTS

[Wyoming protocol](https://github.com/OHF-Voice/wyoming) server for
[OpenRouter](https://openrouter.ai/)'s speech-to-text (STT) and
text-to-speech (TTS) APIs, for use with [Home Assistant](https://www.home-assistant.io/).

Modeled after [wyoming_bluetts](https://github.com/snabb/wyoming_bluetts),
which this project follows for its overall structure (pip package + Home
Assistant app packaging, event handler design).

## Features

- **Multiple independent tasks**: configure any number of STT and/or TTS
  tasks, each its own OpenRouter model/voice/parameters, each listening on
  its own dedicated Wyoming port. Add one "Wyoming Protocol" integration per
  task in Home Assistant.
- **Any OpenRouter STT/TTS model**: configurable per task, no code changes
  needed for new models. At startup the server fetches and logs the live
  OpenRouter STT and TTS model catalogs with current prices.
- **Streaming TTS delivery**: audio is streamed to the client as it's
  received from OpenRouter (`AudioChunk` events emitted incrementally),
  rather than waiting for the whole clip. Whether this is genuinely
  progressive depends on the model/provider -- some backends buffer the
  full clip server-side regardless.
- **mp3-to-PCM decoding**: not every OpenRouter TTS model supports
  `response_format=pcm` (Wyoming's only usable format); tasks can opt into
  requesting `mp3` instead (smaller transfer, useful on a slow link to
  OpenRouter, or required for a model that doesn't support pcm at all) --
  decoded locally to PCM via `mpg123` before Wyoming delivery.
- **Cost and latency logged per request**, tagged with the task name.
- **Home Assistant sensor entities** per task (request count, cumulative
  cost, last/average latency), pushed automatically when running as the
  Home Assistant App (via the Supervisor's Core API proxy -- no MQTT broker
  or separate integration needed).
- No local ML inference, no GPU, no model downloads: a thin async HTTP
  client (plus `mpg123` for the occasional mp3-only TTS model).
- Ships both as a pip-installable Python package and a Home Assistant app.

## How it works

OpenRouter's transcription API (`POST /api/v1/audio/transcriptions`) and
speech API (`POST /api/v1/audio/speech`) each handle one request/response at
a time -- there's no bidirectional streaming protocol to speak natively.
Home Assistant's own Wyoming integration only ever reads the *first*
`AsrProgram`/`TtsProgram` from a Wyoming connection and creates exactly one
entity per connection, so **each configured task listens on its own TCP
port** and shows up as its own entity once you add a "Wyoming Protocol"
integration for that port in Home Assistant. The Home Assistant App/Docker
image reserve ports 10300-10319 by default (20 tasks); there's no cap when
running standalone (e.g. `--config` directly), any free port works.

## Quick start

### Home Assistant app

Settings → Apps → Install app → ⋮ (three dots) → Repositories → Add this
repository's URL, then install "Wyoming OpenRouter" from the store. Configure
one or more tasks in the app's Configuration tab (each with its own name,
API key, type, port, and model). Add one "Wyoming Protocol" integration entry
per task in Home Assistant (Settings → Devices & Services → Add integration).

### Standalone (uv)

```bash
git clone https://github.com/snabb/wyoming_openrouter.git
uv tool install ./wyoming_openrouter
wyoming-openrouter --config tasks.json --debug
```

### Docker

```bash
docker run --rm -p 10300-10319:10300-10319 \
  -v ./tasks.json:/config/tasks.json:ro \
  -e CONFIG_PATH_OVERRIDE=/config/tasks.json \
  ghcr.io/snabb/wyoming_openrouter:latest
```

See [docker-compose.yml](docker-compose.yml) for a persistent deployment
example. Add each task in Home Assistant via Settings → Devices & Services →
Add integration → "Wyoming Protocol" (one entry per task/port).

## Configuration

A single JSON config file (`--config <path>`, or Supervisor's own
`/data/options.json` when running as the HA App) defines a flat list of
tasks -- no separate "service" grouping; each task is fully self-contained,
including its own API key (reuse the same key across tasks by repeating the
value):

```jsonc
{
  "tasks": [
    {
      "name": "kitchen-stt",
      "api_key": "sk-or-v1-...",
      "type": "stt",
      "port": 10300,
      "model": "openai/gpt-4o-mini-transcribe",
      "languages": ["en", "fi"]
    },
    {
      "name": "assist-tts",
      "api_key": "sk-or-v1-...",
      "type": "tts",
      "port": 10301,
      "model": "hexgrad/kokoro-82m",
      "voice": "af_nova",
      "speed": 1.0
    }
  ]
}
```

| Field | Applies to | Description |
|---|---|---|
| `name` | all | Any label; used in logs and HA sensor entity IDs |
| `api_key` | all | Your OpenRouter API key |
| `type` | all | `stt` or `tts` |
| `port` | all | `10300`-`10309`, must be unique |
| `model` | all | Any OpenRouter model slug -- check the startup log for the live catalog + prices |
| `timeout` | all | HTTP timeout in seconds (default `60`) |
| `provider` | all | Advanced: raw JSON string, passed through as OpenRouter's `provider` field |
| `languages` | stt, tts | Required JSON list for standalone STT tasks; advertised to Home Assistant as the task's supported languages. Optional for TTS (defaults to `en`). In the Home Assistant App UI, enter the same values as a comma-separated string such as `en,fi,de` |
| `default_language` | stt | Hint sent to OpenRouter only when a request doesn't specify one; empty lets the model auto-detect |
| `temperature` | stt | `0`-`1`, sampling parameter |
| `voice` | tts | Required; valid values are model-specific -- see the startup log. To offer several voices for the same model, configure one task per voice (each gets its own port/HA entity) |
| `speed` | tts | Playback speed multiplier (default `1.0`) |
| `audio_format` | tts | `pcm` (default, no local decode) or `mp3` (smaller transfer, decoded locally via `mpg123`; needed for models that don't support pcm) |

No cap on the number of tasks; the Home Assistant App/Docker image reserve
20 ports (10300-10319) by default.

```bash
curl -s 'https://openrouter.ai/api/v1/models?output_modalities=transcription' | jq '.data[] | {id, pricing}'
curl -s 'https://openrouter.ai/api/v1/models?output_modalities=speech' | jq '.data[] | {id, pricing, supported_voices}'
```

### Provider passthrough example

OpenAI's TTS models support an `instructions` parameter (accent, emotion,
pacing, tone, etc.), but on OpenRouter this isn't a top-level field -- it's
nested under `provider.options.openai.instructions`. The generic `provider`
field above already forwards arbitrary JSON verbatim, so this works today
with no code changes:

```jsonc
{
  "name": "assist-tts",
  "api_key": "sk-or-v1-...",
  "type": "tts",
  "port": 10301,
  "model": "openai/gpt-4o-mini-tts",
  "voice": "alloy",
  "provider": "{\"options\":{\"openai\":{\"instructions\":\"Speak in a warm, cheerful tone.\"}}}"
}
```

This is specific to whichever vendor OpenRouter actually routes the model
to (`options.openai` here) -- unrelated to the top-level `voice`/`speed`
fields, and not guaranteed to exist for every provider/model.

## Metrics in Home Assistant

When running as the Home Assistant App, five sensor entities are pushed
per task after every request (`<type>` is `stt`/`tts`, `<task>` is the
task's slugified name):

- `sensor.wyoming_openrouter_<type>_<task>_request_count`
- `sensor.wyoming_openrouter_<type>_<task>_total_cost`
- `sensor.wyoming_openrouter_<type>_<task>_unknown_cost_count`
- `sensor.wyoming_openrouter_<type>_<task>_last_latency_ms`
- `sensor.wyoming_openrouter_<type>_<task>_avg_latency_ms`

This uses Home Assistant's own Core API through the Supervisor proxy (the
app's `homeassistant_api: true` manifest option), not MQTT. In standalone
Docker there's no Supervisor to push to, so these are skipped -- request
count/cost/latency are still visible in the container's log lines, each
tagged with the task name.

TTS responses carry no inline cost (unlike STT's `usage.cost`); cost is
resolved asynchronously after audio delivery via OpenRouter's
`/generation?id=...` endpoint (with retry, since there's a real propagation
delay), falling back to a per-character estimate for models confirmed to be
priced that way, or tracked as `unknown_cost_count` rather than a silently
wrong guess for models that aren't (e.g. ones priced by output tokens
instead of input characters).

## Limitations

- No true bidirectional streaming: each request is one OpenRouter API call.
  TTS output is *relayed* incrementally as bytes arrive, but whether
  OpenRouter's backend actually generates progressively (vs. buffering the
  whole clip before responding) varies by model/provider.
- Not every OpenRouter TTS model supports `response_format=pcm` -- use
  `audio_format: mp3` for those (check the server's error log for a model
  rejecting a request; OpenRouter's own error message is logged verbatim).
- Metrics sensor entities are plain Home Assistant states pushed via the
  Core API, not full registry-managed entities tied to a device/config
  entry.
- OpenRouter's transcription endpoint accepts an OpenAI-style `prompt`
  field (for vocabulary/context hints) but documents it as silently
  ignored -- there's currently no way to bias STT transcription with
  context text through OpenRouter.

## Development

```bash
uv sync --all-extras --dev
prek run --all-files
uv run pytest
```

See [CONTRIBUTING.md](CONTRIBUTING.md).

### Model compatibility matrix

`scripts/model_matrix.py` is a standalone tool (not part of the test suite
or CI, since it costs real money and takes real time) that tests every
combination of live OpenRouter STT and TTS models against each other over
the real Wyoming protocol: synthesize a phrase with each TTS model,
transcribe it back with each STT model, and report pass/fail plus cost and
latency. Requires `mpg123` installed locally (already in the Docker image).

```bash
uv run python scripts/model_matrix.py --dry-run
uv run python scripts/model_matrix.py --stt-models openai/whisper-1 --tts-models hexgrad/kokoro-82m
```

## License

MIT. Uses the [Wyoming protocol](https://github.com/OHF-Voice/wyoming) (MIT).
