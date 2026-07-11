"""Tests for wyoming_openrouter.tts_handler."""

import asyncio
import logging
from unittest.mock import AsyncMock, patch

from wyoming.audio import AudioChunk
from wyoming.info import Describe, Info
from wyoming.tts import Synthesize, SynthesizeVoice

from wyoming_openrouter.config import plan_tasks
from wyoming_openrouter.ha_metrics import Metrics
from wyoming_openrouter.openrouter import SpeechMeta
from wyoming_openrouter.tts_handler import (
    OpenRouterTtsEventHandler,
    get_tts_wyoming_info,
)


def _task(**overrides):
    raw = {
        "name": "assist-tts",
        "api_key": "sk-test",
        "type": "tts",
        "port": 10301,
        "model": "openai/gpt-4o-mini-tts",
        "voice": "alloy",
    }
    raw.update(overrides)
    return plan_tasks({"tasks": [raw]})[0]


def _run(coro):
    return asyncio.run(coro)


class _RecordingHandler(OpenRouterTtsEventHandler):
    """Handler whose write_event records events instead of touching a socket."""

    def __init__(self, task=None, tts_pricing=None):
        # Bypass AsyncEventHandler.__init__ (needs a reader/writer); set only
        # what handle_event uses.
        self.task = task or _task()
        self.wyoming_info = get_tts_wyoming_info(self.task)
        self.metrics = Metrics(task_type="tts", task_slug=self.task.slug)
        self.tts_pricing = tts_pricing or {}
        self._logger = logging.getLogger(f"wyoming_openrouter.task.{self.task.slug}")
        self._background_tasks: set = set()
        self.written: list = []

    async def write_event(self, event):
        self.written.append(event)


def _fake_meta(rate=24000, width=2, channels=1, generation_id="gen-1"):
    return SpeechMeta(
        rate=rate, width=width, channels=channels, generation_id=generation_id
    )


async def _await_background(handler):
    if handler._background_tasks:
        await asyncio.gather(*handler._background_tasks)


async def _run_synthesize(handler, text="hello there", voice_override=None):
    voice = SynthesizeVoice(name=voice_override) if voice_override else None
    await handler.handle_event(Synthesize(text=text, voice=voice).event())
    await _await_background(handler)


# --- Describe / info ------------------------------------------------------------


def test_describe_returns_info():
    handler = _RecordingHandler()
    result = _run(handler.handle_event(Describe().event()))

    assert result is True
    assert len(handler.written) == 1
    assert Info.is_type(handler.written[0].type)


def test_get_tts_wyoming_info_single_voice():
    task = _task(voice="af_nova")
    info = get_tts_wyoming_info(task)
    assert len(info.tts) == 1
    assert info.tts[0].voices[0].name == "af_nova"
    assert info.tts[0].supports_synthesize_streaming is True


def test_get_tts_wyoming_info_defaults_to_english_when_no_language_set():
    # A voice with an empty languages list crashes HA's tts entity setup
    # entirely (AttributeError on default_language) -- must never be empty.
    task = _task(voice="af_nova")
    assert task.language is None
    info = get_tts_wyoming_info(task)
    assert info.tts[0].voices[0].languages == ["en"]


def test_get_tts_wyoming_info_honors_language_override():
    task = _task(voice="fr_marie_neutral", language="fr")
    info = get_tts_wyoming_info(task)
    assert info.tts[0].voices[0].languages == ["fr"]


# --- full synthesize flow --------------------------------------------------------


def test_full_flow_streams_audio_start_chunks_stop_and_synthesize_stopped():
    meta = _fake_meta()
    chunks = [b"\x00\x01" * 100, b"\x02\x03" * 100]

    handler = _RecordingHandler()
    with (
        patch(
            "wyoming_openrouter.tts_handler.openrouter.synthesize_stream",
            return_value=(meta, iter(chunks)),
        ),
        patch(
            "wyoming_openrouter.tts_handler.openrouter.get_generation_cost",
            return_value=0.0001,
        ),
        patch("wyoming_openrouter.tts_handler.push_to_supervisor", new=AsyncMock()),
        patch("wyoming_openrouter.tts_handler.asyncio.sleep", new=AsyncMock()),
    ):
        _run(_run_synthesize(handler))

    types = [e.type for e in handler.written]
    assert types == [
        "audio-start",
        "audio-chunk",
        "audio-chunk",
        "audio-stop",
        "synthesize-stopped",
    ]
    audio_chunks = [e for e in handler.written if AudioChunk.is_type(e.type)]
    assert [AudioChunk.from_event(e).audio for e in audio_chunks] == chunks
    assert handler.metrics.request_count == 1
    assert handler.metrics.total_cost == 0.0001


def test_empty_text_sends_clean_empty_response():
    handler = _RecordingHandler()
    with patch(
        "wyoming_openrouter.tts_handler.openrouter.synthesize_stream"
    ) as mock_synth:
        _run(_run_synthesize(handler, text="   "))

    mock_synth.assert_not_called()
    types = [e.type for e in handler.written]
    assert types == ["audio-start", "audio-stop", "synthesize-stopped"]
    assert handler.metrics.request_count == 0


def test_mid_stream_exception_still_ends_in_audio_stop_and_synthesize_stopped():
    meta = _fake_meta()

    def _raising_iter():
        yield b"\x00\x01"
        raise RuntimeError("boom")

    handler = _RecordingHandler()
    with (
        patch(
            "wyoming_openrouter.tts_handler.openrouter.synthesize_stream",
            return_value=(meta, _raising_iter()),
        ),
        patch(
            "wyoming_openrouter.tts_handler.openrouter.get_generation_cost",
            return_value=0.0001,
        ),
        patch("wyoming_openrouter.tts_handler.push_to_supervisor", new=AsyncMock()),
        patch("wyoming_openrouter.tts_handler.asyncio.sleep", new=AsyncMock()),
    ):
        _run(_run_synthesize(handler))

    types = [e.type for e in handler.written]
    assert types[-2:] == ["audio-stop", "synthesize-stopped"]


def test_mp3_audio_format_decodes_via_mp3_decode_and_uses_fixed_output_format():
    # meta.rate/width/channels deliberately mismatch the fixed mp3-decode
    # output format, to prove the mp3 branch ignores them (they describe
    # OpenRouter's pcm Content-Type header, meaningless for an mp3 response).
    meta = _fake_meta(rate=999, width=1, channels=2)
    task = _task(audio_format="mp3")
    handler = _RecordingHandler(task=task)

    with (
        patch(
            "wyoming_openrouter.tts_handler.openrouter.synthesize_stream",
            return_value=(meta, iter([b"mp3-bytes"])),
        ) as mock_synth,
        patch(
            "wyoming_openrouter.tts_handler.mp3_decode.decode_mp3_stream",
            return_value=iter([b"\x00\x01\x02\x03"]),
        ) as mock_decode,
        patch(
            "wyoming_openrouter.tts_handler.openrouter.get_generation_cost",
            return_value=0.0001,
        ),
        patch("wyoming_openrouter.tts_handler.push_to_supervisor", new=AsyncMock()),
        patch("wyoming_openrouter.tts_handler.asyncio.sleep", new=AsyncMock()),
    ):
        _run(_run_synthesize(handler))

    assert mock_synth.call_args.args[-1] == "mp3"  # response_format passed through
    assert mock_decode.call_count == 1

    from wyoming.audio import AudioStart

    start_events = [e for e in handler.written if AudioStart.is_type(e.type)]
    assert len(start_events) == 1
    start = AudioStart.from_event(start_events[0])
    assert (start.rate, start.width, start.channels) == (24000, 2, 1)

    chunk_events = [e for e in handler.written if AudioChunk.is_type(e.type)]
    assert AudioChunk.from_event(chunk_events[0]).audio == b"\x00\x01\x02\x03"


def test_voice_override_from_synthesize_beats_task_default():
    meta = _fake_meta()
    handler = _RecordingHandler()
    with (
        patch(
            "wyoming_openrouter.tts_handler.openrouter.synthesize_stream",
            return_value=(meta, iter([b"\x00\x01"])),
        ) as mock_synth,
        patch(
            "wyoming_openrouter.tts_handler.openrouter.get_generation_cost",
            return_value=0.0001,
        ),
        patch("wyoming_openrouter.tts_handler.push_to_supervisor", new=AsyncMock()),
        patch("wyoming_openrouter.tts_handler.asyncio.sleep", new=AsyncMock()),
    ):
        _run(_run_synthesize(handler, voice_override="bf_emma"))

    args, _ = mock_synth.call_args
    # synthesize_stream(api_key, model, text, voice, speed, provider, timeout)
    assert args[3] == "bf_emma"


# --- cost resolution --------------------------------------------------------------


def test_cost_resolves_promptly():
    meta = _fake_meta()
    handler = _RecordingHandler()
    with (
        patch(
            "wyoming_openrouter.tts_handler.openrouter.synthesize_stream",
            return_value=(meta, iter([b"\x00\x01"])),
        ),
        patch(
            "wyoming_openrouter.tts_handler.openrouter.get_generation_cost",
            return_value=0.00042,
        ) as mock_cost,
        patch(
            "wyoming_openrouter.tts_handler.push_to_supervisor", new=AsyncMock()
        ) as mock_push,
        patch(
            "wyoming_openrouter.tts_handler.asyncio.sleep", new=AsyncMock()
        ) as mock_sleep,
    ):
        _run(_run_synthesize(handler))

    assert mock_cost.call_count == 1
    assert mock_sleep.call_count == 1  # only the first retry delay before it resolved
    assert handler.metrics.total_cost == 0.00042
    assert handler.metrics.unknown_cost_count == 0
    mock_push.assert_awaited_once()


def test_cost_falls_back_to_estimate_for_reliable_model_when_lookup_never_resolves():
    meta = _fake_meta()
    handler = _RecordingHandler(tts_pricing={"openai/gpt-4o-mini-tts": 0.00001})
    with (
        patch(
            "wyoming_openrouter.tts_handler.openrouter.synthesize_stream",
            return_value=(meta, iter([b"\x00\x01"])),
        ),
        patch(
            "wyoming_openrouter.tts_handler.openrouter.get_generation_cost",
            return_value=None,
        ) as mock_cost,
        patch("wyoming_openrouter.tts_handler.push_to_supervisor", new=AsyncMock()),
        patch("wyoming_openrouter.tts_handler.asyncio.sleep", new=AsyncMock()),
    ):
        _run(_run_synthesize(handler, text="12345"))

    assert mock_cost.call_count == 4  # exhausted all retries
    assert handler.metrics.total_cost == 0.00001 * 5
    assert handler.metrics.unknown_cost_count == 0


def test_cost_recorded_as_unknown_for_outlier_model_when_lookup_never_resolves():
    meta = _fake_meta()
    # tts_pricing intentionally missing this model -- simulates an outlier
    # (e.g. nonzero pricing.completion) or a failed catalog fetch at startup.
    handler = _RecordingHandler(tts_pricing={})
    with (
        patch(
            "wyoming_openrouter.tts_handler.openrouter.synthesize_stream",
            return_value=(meta, iter([b"\x00\x01"])),
        ),
        patch(
            "wyoming_openrouter.tts_handler.openrouter.get_generation_cost",
            return_value=None,
        ),
        patch("wyoming_openrouter.tts_handler.push_to_supervisor", new=AsyncMock()),
        patch("wyoming_openrouter.tts_handler.asyncio.sleep", new=AsyncMock()),
    ):
        _run(_run_synthesize(handler))

    assert handler.metrics.total_cost == 0.0
    assert handler.metrics.unknown_cost_count == 1


def test_missing_generation_id_records_request_with_unknown_cost():
    meta = _fake_meta(generation_id=None)
    handler = _RecordingHandler(tts_pricing={})
    with (
        patch(
            "wyoming_openrouter.tts_handler.openrouter.synthesize_stream",
            return_value=(meta, iter([b"\x00\x01"])),
        ),
        patch(
            "wyoming_openrouter.tts_handler.openrouter.get_generation_cost"
        ) as mock_cost,
        patch(
            "wyoming_openrouter.tts_handler.push_to_supervisor", new=AsyncMock()
        ) as mock_push,
    ):
        _run(_run_synthesize(handler))

    mock_cost.assert_not_called()
    assert handler.metrics.request_count == 1
    assert handler.metrics.unknown_cost_count == 1
    mock_push.assert_awaited_once()


def test_audio_stop_and_synthesize_stopped_written_before_cost_resolution_completes():
    meta = _fake_meta()
    handler = _RecordingHandler()

    async def _scenario():
        with (
            patch(
                "wyoming_openrouter.tts_handler.openrouter.synthesize_stream",
                return_value=(meta, iter([b"\x00\x01"])),
            ),
            patch(
                "wyoming_openrouter.tts_handler.openrouter.get_generation_cost",
                return_value=0.0001,
            ),
            patch("wyoming_openrouter.tts_handler.push_to_supervisor", new=AsyncMock()),
            patch("wyoming_openrouter.tts_handler.asyncio.sleep", new=AsyncMock()),
        ):
            await handler.handle_event(Synthesize(text="hi").event())
            # The outer call has already returned here, but the
            # fire-and-forget cost-resolution task hasn't necessarily run yet
            # -- proving AudioStop/SynthesizeStopped don't wait on it.
            types = [e.type for e in handler.written]
            assert types[-2:] == ["audio-stop", "synthesize-stopped"]
            assert handler.metrics.request_count == 0  # not yet recorded

            await _await_background(handler)
            assert handler.metrics.request_count == 1  # now recorded

    _run(_scenario())


def test_logger_name_includes_task_slug():
    task = _task(name="Office TTS")
    handler = _RecordingHandler(task=task)
    assert handler._logger.name == "wyoming_openrouter.task.office_tts"
