# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-07-06

### Added

- Initial release: Wyoming protocol server for OpenRouter speech-to-text.
- Configurable OpenRouter STT models (`--models`, comma-separated; first is
  the default), each advertised as a separate `AsrModel`. A Wyoming client
  that sets `Transcribe.name` to a configured slug gets that model for that
  request; Home Assistant's own client never sets this field, so it always
  gets the first configured model.
- Logs the live OpenRouter STT model catalog with current prices at startup,
  so users can pick a `--models` value without leaving the app's log tab.
- Combined per-request log line with model, language, audio duration,
  latency, and cost (`usage.cost` from OpenRouter's response, which is
  authoritative regardless of whether the model is duration- or
  token-priced).
- One quick retry (short fixed delay) on HTTP 429/502/503/504 from
  OpenRouter, then gives up and returns a clean empty transcript rather than
  a hard error -- keeps voice-assistant latency bounded.
- Home Assistant sensor entities (request count, cumulative cost, last/avg
  latency) pushed via the Supervisor's Core API proxy
  (`homeassistant_api: true` + `SUPERVISOR_TOKEN`) when running as the HA
  App; skipped in standalone Docker.
- Home Assistant app packaging alongside the pip-installable package.
- Single Alpine-based Dockerfile, built for amd64 and arm64 by CI using
  native runners (no QEMU).
