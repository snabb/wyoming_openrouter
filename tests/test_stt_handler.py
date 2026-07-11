"""Tests for wyoming_openrouter.stt_handler."""

import asyncio
import logging
from unittest.mock import AsyncMock, patch

from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.info import Describe, Info
from wyoming_openrouter.config import plan_tasks
from wyoming_openrouter.ha_metrics import Metrics
from wyoming_openrouter.openrouter import TranscriptionResult
from wyoming_openrouter.stt_handler import (
    OpenRouterSttEventHandler,
    get_stt_wyoming_info,
)


def _task(**overrides):
    raw = {
        "name": "kitchen-stt",
        "api_key": "sk-test",
        "type": "stt",
        "port": 10300,
        "model": "openai/gpt-4o-mini-transcribe",
        "languages": ["en"],
    }
    raw.update(overrides)
    return plan_tasks({"tasks": [raw]})[0]


def _run(coro):
    return asyncio.run(coro)


class _RecordingHandler(OpenRouterSttEventHandler):
    """Handler whose write_event records events instead of touching a socket."""

    def __init__(self, task=None):
        # Bypass AsyncEventHandler.__init__ (needs a reader/writer); set only
        # what handle_event uses, mirroring the real __init__'s per-task
        # logger construction exactly (not a stand-in) so its naming is
        # actually exercised.
        self.task = task or _task()
        self.wyoming_info = get_stt_wyoming_info(self.task)
        self.metrics = Metrics(task_type="stt", task_slug=self.task.slug)
        self._logger = logging.getLogger(f"wyoming_openrouter.task.{self.task.slug}")
        self._audio_chunks: list = []
        self._audio_rate = 16000
        self._audio_width = 2
        self._audio_channels = 1
        self._requested_language = None
        self.written: list = []

    async def write_event(self, event):
        self.written.append(event)


async def _send_utterance(handler, pcm=b"\x01\x00" * 8000, language=None):
    await handler.handle_event(Transcribe(language=language).event())
    await handler.handle_event(AudioStart(rate=16000, width=2, channels=1).event())
    await handler.handle_event(
        AudioChunk(audio=pcm, rate=16000, width=2, channels=1).event()
    )
    await handler.handle_event(AudioStop().event())


# --- Describe -----------------------------------------------------------------


def test_describe_returns_info():
    handler = _RecordingHandler()
    result = _run(handler.handle_event(Describe().event()))

    assert result is True
    assert len(handler.written) == 1
    assert Info.is_type(handler.written[0].type)


# --- get_stt_wyoming_info -------------------------------------------------------


def test_get_stt_wyoming_info_single_model():
    task = _task(languages=["es", "fi"])
    info = get_stt_wyoming_info(task)
    assert len(info.asr) == 1
    assert len(info.asr[0].models) == 1
    assert info.asr[0].models[0].name == task.model
    assert info.asr[0].models[0].languages == ["es", "fi"]


# --- full transcription flow ----------------------------------------------------


def test_full_flow_produces_one_transcript_with_mocked_text():
    fake_result = TranscriptionResult(
        text="hello there", cost=0.0002, elapsed_ms=123, usage={"cost": 0.0002}
    )
    handler = _RecordingHandler()

    with (
        patch(
            "wyoming_openrouter.stt_handler.openrouter.transcribe",
            return_value=fake_result,
        ) as mock_transcribe,
        patch(
            "wyoming_openrouter.stt_handler.push_to_supervisor", new=AsyncMock()
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
        patch(
            "wyoming_openrouter.stt_handler.openrouter.transcribe"
        ) as mock_transcribe,
        patch(
            "wyoming_openrouter.stt_handler.push_to_supervisor", new=AsyncMock()
        ) as mock_push,
    ):
        _run(_send_utterance(handler, pcm=b""))

    mock_transcribe.assert_not_called()
    mock_push.assert_not_awaited()
    transcripts = [e for e in handler.written if Transcript.is_type(e.type)]
    assert len(transcripts) == 1
    assert Transcript.from_event(transcripts[0]).text == ""
    assert handler.metrics.request_count == 0


def test_task_model_and_params_passed_through():
    fake_result = TranscriptionResult(text="ok", cost=0.0, elapsed_ms=1, usage={})
    task = _task(
        model="openai/whisper-1", temperature=0.2, provider='{"order":["openai"]}'
    )
    handler = _RecordingHandler(task=task)

    with (
        patch(
            "wyoming_openrouter.stt_handler.openrouter.transcribe",
            return_value=fake_result,
        ) as mock_transcribe,
        patch("wyoming_openrouter.stt_handler.push_to_supervisor", new=AsyncMock()),
    ):
        _run(_send_utterance(handler))

    args, _ = mock_transcribe.call_args
    # transcribe(api_key, model, wav_bytes, language, temperature, provider, timeout)
    assert args[1] == "openai/whisper-1"
    assert args[4] == 0.2
    assert args[5] == {"order": ["openai"]}


def test_language_passthrough_from_transcribe():
    fake_result = TranscriptionResult(text="ok", cost=0.0, elapsed_ms=1, usage={})
    handler = _RecordingHandler()

    with (
        patch(
            "wyoming_openrouter.stt_handler.openrouter.transcribe",
            return_value=fake_result,
        ) as mock_transcribe,
        patch("wyoming_openrouter.stt_handler.push_to_supervisor", new=AsyncMock()),
    ):
        _run(_send_utterance(handler, language="es"))

    args, _ = mock_transcribe.call_args
    assert args[3] == "es"


def test_default_language_used_when_transcribe_omits_it():
    fake_result = TranscriptionResult(text="ok", cost=0.0, elapsed_ms=1, usage={})
    task = _task(default_language="de")
    handler = _RecordingHandler(task=task)

    with (
        patch(
            "wyoming_openrouter.stt_handler.openrouter.transcribe",
            return_value=fake_result,
        ) as mock_transcribe,
        patch("wyoming_openrouter.stt_handler.push_to_supervisor", new=AsyncMock()),
    ):
        _run(_send_utterance(handler, language=None))

    args, _ = mock_transcribe.call_args
    assert args[3] == "de"


def test_api_failure_yields_clean_empty_transcript():
    handler = _RecordingHandler()

    with (
        patch(
            "wyoming_openrouter.stt_handler.openrouter.transcribe",
            side_effect=RuntimeError("boom"),
        ),
        patch(
            "wyoming_openrouter.stt_handler.push_to_supervisor", new=AsyncMock()
        ) as mock_push,
    ):
        _run(_send_utterance(handler))

    transcripts = [e for e in handler.written if Transcript.is_type(e.type)]
    assert len(transcripts) == 1
    assert Transcript.from_event(transcripts[0]).text == ""
    assert handler.metrics.request_count == 0
    mock_push.assert_not_awaited()


def test_logger_name_includes_task_slug():
    task = _task(name="Living Room STT")
    handler = _RecordingHandler(task=task)
    assert handler._logger.name == "wyoming_openrouter.task.living_room_stt"
