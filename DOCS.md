# Home Assistant App: Wyoming OpenRouter

Speech-to-text using [OpenRouter](https://openrouter.ai/)'s hosted STT
models. Requires your own OpenRouter API key; every transcription request is
billed against your OpenRouter account.

## Installation

1. Settings → Apps → Install app → ⋮ (three dots) → Repositories → add this
   repository's URL.
2. Find "Wyoming OpenRouter" in the store and install it.
3. Open the Configuration tab and set your `api_key`
   ([openrouter.ai/keys](https://openrouter.ai/keys)).
4. Start the app. Check the Log tab: it prints the live OpenRouter STT model
   catalog with current prices, useful for choosing a `models` value.
5. Home Assistant auto-discovers it via the Wyoming protocol. Assign it as
   the STT provider for your voice assistant under Settings → Voice
   assistants.

## Configuration options

| Option | Default | Description |
|---|---|---|
| `api_key` | *(required)* | Your OpenRouter API key |
| `models` | `openai/gpt-4o-mini-transcribe` | Comma-separated OpenRouter STT model slugs; the first is the default used for every Home Assistant request |
| `languages` | `en` | Languages to advertise to Home Assistant's UI (not enforced by the server) |
| `default_language` | *(empty)* | Optional ISO-639-1 hint sent to OpenRouter when a request doesn't specify one; empty lets the model auto-detect |
| `timeout` | `60` | HTTP timeout (seconds) for OpenRouter requests |
| `debug` | `false` | Verbose logging |

## Choosing a model

The app's Log tab shows the current live OpenRouter STT catalog and prices at
startup. As of this writing, options include (prices change -- check the log
for current values):

| Model | Notes |
|---|---|
| `openai/gpt-4o-mini-transcribe` | Default. Cheap, modern, good accuracy. |
| `openai/whisper-1` | The long-established baseline. |
| `mistralai/voxtral-mini-transcribe` | Cheap alternative. |
| `qwen/qwen3-asr-flash-...` | Very cheap, newer provider. |

## Metrics

This app pushes four sensor entities to Home Assistant after every
transcription request, using Home Assistant's own Core API (via the
Supervisor proxy, not MQTT):

- `sensor.wyoming_openrouter_request_count`
- `sensor.wyoming_openrouter_total_cost` (USD)
- `sensor.wyoming_openrouter_last_latency_ms`
- `sensor.wyoming_openrouter_avg_latency_ms`

## Troubleshooting

- **App won't start / "No OpenRouter API key configured"**: set `api_key` in
  the Configuration tab.
- **Transcripts come back empty**: check the app's log for the per-request
  `Transcribe: model=... latency=...ms cost=$...` line and any
  `OpenRouter transcription request failed` errors just above it -- a
  transient OpenRouter failure returns an empty transcript rather than an
  error, by design.
- **Model doesn't exist / 400 errors**: check the current model slug against
  the live catalog logged at startup, or
  [openrouter.ai/models](https://openrouter.ai/models?output_modalities=transcription).
- **No sensor entities appearing**: metrics are only pushed when running as
  this Home Assistant App (they need the Supervisor's `SUPERVISOR_TOKEN`);
  standalone Docker installs won't have them.
