# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-07-06

### Added

- Initial release: Wyoming protocol server for OpenRouter speech-to-text and
  text-to-speech.
- Multi-task configuration: a flat `tasks` list (no separate "service"
  grouping; each task is fully self-contained, including its own API key),
  each task an independent STT or TTS job with its own model/parameters,
  listening on its own dedicated Wyoming port (`10300`-`10309`, up to 10
  tasks) -- required since Home Assistant's built-in wyoming integration
  only ever reads the first program from a connection and creates one
  entity per connection.
- STT: model, language, default-language hint, temperature, and raw
  `provider` passthrough are all configurable per task.
- TTS: model, voice, speed, and raw `provider` passthrough are configurable
  per task. Audio is streamed to the client incrementally (`AudioChunk`
  events emitted as bytes arrive from OpenRouter, via a background-thread-
  to-asyncio-queue bridge) rather than buffered whole before delivery,
  though whether this is genuinely progressive depends on the model/
  provider's own backend behavior.
- `audio_format` (TTS, `pcm` default or `mp3`): not every OpenRouter TTS
  model supports `response_format=pcm` (Wyoming's only usable format) --
  `mp3` requests a compressed response instead (also useful on a slow link
  to OpenRouter even for pcm-capable models) and decodes it locally to PCM
  via `mpg123` before Wyoming delivery.
- TTS cost tracking: OpenRouter's speech response carries no inline cost
  (unlike STT's `usage.cost`), so cost is resolved asynchronously after
  audio delivery via a retrying `/generation?id=...` lookup, falling back to
  a per-character estimate only for models confirmed priced that way, and
  tracked as "unknown" (not silently guessed) for models that aren't (e.g.
  ones priced by output tokens instead of input characters).
- Logs the live OpenRouter STT and TTS model catalogs with current prices at
  startup, so users can pick a task's `model`/`voice` without leaving the
  app's log tab.
- Combined per-request log line with model, language/voice, audio duration,
  latency, and cost, tagged with the task name via a per-task logger.
- One quick retry (short fixed delay) on HTTP 429/502/503/504 from
  OpenRouter, then gives up and returns a clean empty response rather than
  a hard error -- keeps voice-assistant latency bounded. OpenRouter's own
  error message is surfaced in full on failure (not just a generic HTTP
  status) for easier troubleshooting.
- Home Assistant sensor entities per task (request count, cumulative cost,
  unknown-cost count, last/avg latency) pushed via the Supervisor's Core API
  proxy (`homeassistant_api: true` + `SUPERVISOR_TOKEN`) when running as the
  HA App; skipped in standalone Docker.
- Home Assistant app packaging alongside the pip-installable package.
- Single Alpine-based Dockerfile, built for amd64 and arm64 by CI using
  native runners (no QEMU). Includes `mpg123` (~1.7 MB) for mp3 decoding --
  chosen over a full ffmpeg install to keep the image small.
