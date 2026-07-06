"""Tests for wyoming_openrouter.handler."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.info import Describe, Info

from wyoming_openrouter.ha_metrics import Metrics
from wyoming_openrouter.handler import OpenRouterEventHandler, get_wyoming_info
from wyoming_openrouter.openrouter import TranscriptionResult

DEFAULT_MODELS = ["openai/gpt-4o-mini-transcribe", "openai/whisper-1"]


def _run(coro):
    return asyncio.run(coro)


class _RecordingHandler(OpenRouterEventHandler):
    """Handler whose write_event records events instead of touching a socket."""

    def __init__(self, models=None, languages=None, default_language=None):
        # Bypass AsyncEventHandler.__init__ (needs a reader/writer); set only
        # what handle_event uses.
        models = models or DEFAULT_MODELS
        self.wyoming_info = get_wyoming_info(models, languages or ["en"])
        self.cli_args = SimpleNamespace(
            api_key="sk-test",
            models=models,
            timeout=30.0,
            default_language=default_language,
        )
        self.metrics = Metrics()
        self._audio_chunks: list = []
        self._audio_rate = 16000
        self._audio_width = 2
        self._audio_channels = 1
        self._requested_language = None
        self._requested_model = models[0]
        self.written: list = []

    async def write_event(self, event):
        self.written.append(event)


async def _send_utterance(handler, pcm=b"\x01\x00" * 8000, model_name=None, language=None):
    await handler.handle_event(Transcribe(name=model_name, language=language).event())
    await handler.handle_event(AudioStart(rate=16000, width=2, channels=1).event())
    await handler.handle_event(AudioChunk(audio=pcm, rate=16000, width=2, channels=1).event())
    await handler.handle_event(AudioStop().event())


# --- Describe -----------------------------------------------------------------


def test_describe_returns_info():
    handler = _RecordingHandler()
    result = _run(handler.handle_event(Describe().event()))

    assert result is True
    assert len(handler.written) == 1
    assert Info.is_type(handler.written[0].type)


# --- get_wyoming_info -----------------------------------------------------------


def test_get_wyoming_info_one_model_per_slug():
    info = get_wyoming_info(DEFAULT_MODELS, ["en", "es"])
    assert len(info.asr) == 1
    assert {m.name for m in info.asr[0].models} == set(DEFAULT_MODELS)
    for model in info.asr[0].models:
        assert model.languages == ["en", "es"]


# --- full transcription flow ----------------------------------------------------


def test_full_flow_produces_one_transcript_with_mocked_text():
    fake_result = TranscriptionResult(
        text="hello there", cost=0.0002, elapsed_ms=123, usage={"cost": 0.0002}
    )
    handler = _RecordingHandler()

    with (
        patch(
            "wyoming_openrouter.handler.openrouter.transcribe", return_value=fake_result
        ) as mock_transcribe,
        patch(
            "wyoming_openrouter.handler.push_to_supervisor", new=AsyncMock()
        ) as mock_push,
    ):
        _run(_send_utterance(handler))

    assert mock_transcribe.call_count == 1
    transcripts = [e for e in handler.written if Transcript.is_type(e.type)]
    assert len(transcripts) == 1
    assert Transcript.from_event(transcripts[0]).text == "hello there"
    assert handler.metrics.request_count == 1
    assert handler.metrics.total_cost == 0.0002
    mock_push.assert_awaited_once()


def test_empty_audio_skips_api_call_and_returns_empty_transcript():
    handler = _RecordingHandler()

    with (
        patch("wyoming_openrouter.handler.openrouter.transcribe") as mock_transcribe,
        patch(
            "wyoming_openrouter.handler.push_to_supervisor", new=AsyncMock()
        ) as mock_push,
    ):
        _run(_send_utterance(handler, pcm=b""))

    mock_transcribe.assert_not_called()
    mock_push.assert_not_awaited()
    transcripts = [e for e in handler.written if Transcript.is_type(e.type)]
    assert len(transcripts) == 1
    assert Transcript.from_event(transcripts[0]).text == ""
    assert handler.metrics.request_count == 0


def test_transcribe_name_matching_configured_model_selects_it():
    fake_result = TranscriptionResult(text="ok", cost=0.0, elapsed_ms=1, usage={})
    handler = _RecordingHandler()

    with (
        patch(
            "wyoming_openrouter.handler.openrouter.transcribe", return_value=fake_result
        ) as mock_transcribe,
        patch("wyoming_openrouter.handler.push_to_supervisor", new=AsyncMock()),
    ):
        _run(_send_utterance(handler, model_name="openai/whisper-1"))

    args, _ = mock_transcribe.call_args
    assert args[1] == "openai/whisper-1"


def test_transcribe_name_unrecognized_falls_back_to_default_model():
    fake_result = TranscriptionResult(text="ok", cost=0.0, elapsed_ms=1, usage={})
    handler = _RecordingHandler()

    with (
        patch(
            "wyoming_openrouter.handler.openrouter.transcribe", return_value=fake_result
        ) as mock_transcribe,
        patch("wyoming_openrouter.handler.push_to_supervisor", new=AsyncMock()),
    ):
        _run(_send_utterance(handler, model_name="does/not-exist"))

    args, _ = mock_transcribe.call_args
    assert args[1] == DEFAULT_MODELS[0]


def test_language_passthrough_from_transcribe():
    fake_result = TranscriptionResult(text="ok", cost=0.0, elapsed_ms=1, usage={})
    handler = _RecordingHandler()

    with (
        patch(
            "wyoming_openrouter.handler.openrouter.transcribe", return_value=fake_result
        ) as mock_transcribe,
        patch("wyoming_openrouter.handler.push_to_supervisor", new=AsyncMock()),
    ):
        _run(_send_utterance(handler, language="es"))

    args, _ = mock_transcribe.call_args
    assert args[3] == "es"


def test_default_language_used_when_transcribe_omits_it():
    fake_result = TranscriptionResult(text="ok", cost=0.0, elapsed_ms=1, usage={})
    handler = _RecordingHandler(default_language="de")

    with (
        patch(
            "wyoming_openrouter.handler.openrouter.transcribe", return_value=fake_result
        ) as mock_transcribe,
        patch("wyoming_openrouter.handler.push_to_supervisor", new=AsyncMock()),
    ):
        _run(_send_utterance(handler, language=None))

    args, _ = mock_transcribe.call_args
    assert args[3] == "de"


def test_api_failure_yields_clean_empty_transcript():
    handler = _RecordingHandler()

    with (
        patch(
            "wyoming_openrouter.handler.openrouter.transcribe",
            side_effect=RuntimeError("boom"),
        ),
        patch(
            "wyoming_openrouter.handler.push_to_supervisor", new=AsyncMock()
        ) as mock_push,
    ):
        _run(_send_utterance(handler))

    transcripts = [e for e in handler.written if Transcript.is_type(e.type)]
    assert len(transcripts) == 1
    assert Transcript.from_event(transcripts[0]).text == ""
    assert handler.metrics.request_count == 0
    mock_push.assert_not_awaited()
