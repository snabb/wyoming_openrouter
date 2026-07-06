# Home Assistant App: Wyoming OpenRouter

Speech-to-text and text-to-speech using [OpenRouter](https://openrouter.ai/)'s
hosted models. Requires your own OpenRouter API key(s); every request is
billed against your OpenRouter account.

## Installation

1. Settings → Apps → Install app → ⋮ (three dots) → Repositories → add this
   repository's URL.
2. Find "Wyoming OpenRouter" in the store and install it.
3. Open the Configuration tab and add one or more **tasks** (see below).
4. Start the app. Check the Log tab: it prints the live OpenRouter STT and
   TTS model catalogs with current prices, useful for choosing a task's
   `model`/`voice`.
5. For each configured task, go to Settings → Devices & Services → Add
   integration → "Wyoming Protocol" and enter this app's address and the
   task's port. One Home Assistant integration entry per task.

## Tasks

Each task is a fully self-contained speech job: its own name, API key, type
(`stt` or `tts`), port, model, and parameters. There's no separate "service"
grouping -- reuse the same API key across tasks by entering it more than
once.

**Why one port per task?** Home Assistant's built-in Wyoming integration only
ever looks at the *first* program in a connection's response and creates one
entity per connection. To get multiple independently-usable STT/TTS entities,
each task needs its own dedicated port (10300-10319, up to 20 tasks in this
packaged app) and its own Home Assistant integration entry.

| Field | Applies to | Description |
|---|---|---|
| `name` | all | Any label; used in logs and in this app's Home Assistant sensor entity IDs |
| `api_key` | all | Your OpenRouter API key |
| `type` | all | `stt` or `tts` |
| `port` | all | `10300`-`10309`, must be unique across all tasks |
| `model` | all | Any OpenRouter model slug -- check the Log tab for the live catalog + prices |
| `timeout` | all | HTTP timeout in seconds (default `60`) |
| `provider` | all | Advanced: raw JSON string, passed through as OpenRouter's `provider` field |
| `language` | stt | Advertised to Home Assistant's UI |
| `default_language` | stt | Hint sent to OpenRouter only when a request doesn't specify one; empty lets the model auto-detect |
| `temperature` | stt | `0`-`1`, sampling parameter |
| `voice` | tts | Required; valid values are model-specific -- check the Log tab |
| `speed` | tts | Playback speed multiplier (default `1.0`) |
| `audio_format` | tts | `pcm` (default) or `mp3` -- see below |

## Choosing a model

The Log tab shows the current live OpenRouter STT and TTS catalogs and
prices at startup. As of this writing (prices and models change -- check the
log for current values):

**STT**: `openai/gpt-4o-mini-transcribe` (cheap, modern), `openai/whisper-1`
(the long-established baseline).

**TTS**: `hexgrad/kokoro-82m` (cheap, many voices, confirmed to support
`pcm` directly), `mistralai/voxtral-mini-tts-2603` (emotion-tagged voices,
**requires `audio_format: mp3`** -- see below).

## pcm vs. mp3 (`audio_format`, TTS tasks only)

Wyoming always needs raw PCM audio internally, but not every OpenRouter TTS
model can produce it directly:

- `pcm` (default): requests raw PCM from OpenRouter directly. No local
  decoding, lowest latency. Some models reject this -- if a task's log shows
  an error like `Mistral TTS only supports response_format="mp3"`, switch
  that task to `audio_format: mp3`.
- `mp3`: requests compressed mp3 from OpenRouter (smaller transfer -- worth
  considering on a slow link to OpenRouter even for a model that supports
  pcm), decoded locally to PCM via `mpg123` before being sent to Home
  Assistant. Required for models that don't support pcm at all.

## Metrics

This app pushes five sensor entities per task to Home Assistant after every
request, using Home Assistant's own Core API (via the Supervisor proxy, not
MQTT). `<type>` is `stt`/`tts`, `<task>` is the task's slugified name:

- `sensor.wyoming_openrouter_<type>_<task>_request_count`
- `sensor.wyoming_openrouter_<type>_<task>_total_cost` (USD)
- `sensor.wyoming_openrouter_<type>_<task>_unknown_cost_count`
- `sensor.wyoming_openrouter_<type>_<task>_last_latency_ms`
- `sensor.wyoming_openrouter_<type>_<task>_avg_latency_ms`

TTS cost is resolved asynchronously after audio delivery (OpenRouter's
speech response carries no inline cost, unlike STT) -- `unknown_cost_count`
tracks requests where cost couldn't be determined, rather than folding a
guess into `total_cost`.

## Troubleshooting

- **App won't start**: check the Log tab for a config error (e.g. duplicate
  ports, a `tts` task missing `voice`, an out-of-range port).
- **A task's log shows "OpenRouter speech/transcription request failed"**:
  the log line includes OpenRouter's own error message -- e.g. a model
  rejecting `response_format=pcm` (switch that task to `audio_format: mp3`)
  or an invalid model/voice name.
- **Transcripts or audio come back empty with no error**: check the log for
  the per-request `Transcribe:`/`Synthesize request:` line and any error
  just above it -- a transient OpenRouter failure returns a clean empty
  response rather than an error, by design.
- **No sensor entities appearing**: metrics are only pushed when running as
  this Home Assistant App (they need the Supervisor's `SUPERVISOR_TOKEN`);
  standalone Docker installs won't have them.
