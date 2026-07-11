# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Replace each task's singular `language` setting with multi-value `languages`
  and advertise every configured language to Home Assistant.
- Configure live speech models with reviewed language capabilities and add
  localized TTS test announcements for English, Finnish, German, French, and
  Spanish.

## [0.2.0] - 2026-07-11

### Added

- `stt_compare.py` for recording one utterance and comparing it across
  configured Wyoming endpoints and OpenRouter STT models.
- Per-field Home Assistant app configuration help, multi-voice setup guidance,
  and documentation of TTS instruction passthrough and STT prompt limitations.
- Release metadata consistency checks across the Python package, server, and
  Home Assistant app versions.

### Changed

- Model-catalog fetching and Home Assistant discovery now run concurrently and
  no longer delay task port binding or container health checks.
- Docker images are built and smoke-tested natively on amd64 and arm64 before
  the multi-architecture release tags are published.
- Wyoming health and readiness probes now use a protocol-aware Python client
  instead of waiting for `nc` timeouts.

### Fixed

- Propagate OpenRouter MP3 stream failures and `mpg123` decode failures instead
  of returning a successful empty audio response.
- Preserve task names containing spaces during Home Assistant discovery.
- Record TTS request metrics when OpenRouter omits the generation ID.
- Reject task-name slug collisions that would overwrite metric sensors.
- Report malformed standalone task configuration as field-specific errors.
- Require usable STT languages and TTS language metadata so Home Assistant can
  create and select the advertised entities reliably.
- Publish the Home Assistant app's exact versioned image tag and prevent future
  releases from publishing before native smoke tests pass.

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
