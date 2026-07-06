"""Wyoming event handler for one OpenRouter speech-to-text task."""

import asyncio
import io
import logging
import wave
from typing import Optional

from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.event import Event
from wyoming.info import AsrModel, AsrProgram, Attribution, Describe, Info
from wyoming.server import AsyncEventHandler

from . import __version__, openrouter
from .config import TaskConfig
from .ha_metrics import Metrics, push_to_supervisor

_ATTRIBUTION = Attribution(name="OpenRouter", url="https://openrouter.ai")


def get_stt_wyoming_info(task: TaskConfig) -> Info:
    """Create Wyoming info describing this task's single dedicated STT model."""
    languages = [task.language] if task.language else []
    return Info(
        asr=[
            AsrProgram(
                name="openrouter",
                attribution=_ATTRIBUTION,
                installed=True,
                description=f"OpenRouter speech-to-text: {task.name}",
                version=__version__,
                models=[
                    AsrModel(
                        name=task.model,
                        attribution=_ATTRIBUTION,
                        installed=True,
                        description=f"OpenRouter STT model: {task.model}",
                        version=None,
                        languages=languages,
                    )
                ],
            )
        ]
    )


def _build_wav(pcm_bytes: bytes, rate: int, width: int, channels: int) -> bytes:
    """Wrap raw PCM bytes in a WAV container for OpenRouter's input_audio format=wav."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(width)
        wav_file.setframerate(rate)
        wav_file.writeframes(pcm_bytes)
    return buffer.getvalue()


class OpenRouterSttEventHandler(AsyncEventHandler):
    """Handle Wyoming ASR events for one task by proxying audio to OpenRouter."""

    def __init__(
        self,
        wyoming_info: Info,
        task: TaskConfig,
        metrics: Metrics,
        *args,
        **kwargs,
    ) -> None:
        """Initialize handler."""
        super().__init__(*args, **kwargs)
        self.wyoming_info = wyoming_info
        self.task = task
        self.metrics = metrics
        self._logger = logging.getLogger(f"wyoming_openrouter.task.{task.slug}")

        self._audio_chunks: list[bytes] = []
        self._audio_rate = 16000
        self._audio_width = 2
        self._audio_channels = 1
        self._requested_language: Optional[str] = None

    async def handle_event(self, event: Event) -> bool:
        """Handle Wyoming events."""
        if Describe.is_type(event.type):
            await self.write_event(self.wyoming_info.event())
            self._logger.debug("Sent info in response to describe")
            return True

        if Transcribe.is_type(event.type):
            transcribe = Transcribe.from_event(event)
            # Home Assistant always sets .language from the Assist pipeline's
            # STT language; pass it straight through. Only fall back to the
            # configured default_language hint when a client omits it
            # entirely -- otherwise leave it unset and let the model
            # auto-detect.
            self._requested_language = transcribe.language or self.task.default_language
            self._audio_chunks = []
            return True

        if AudioStart.is_type(event.type):
            audio_start = AudioStart.from_event(event)
            self._audio_rate = audio_start.rate
            self._audio_width = audio_start.width
            self._audio_channels = audio_start.channels
            self._audio_chunks = []
            return True

        if AudioChunk.is_type(event.type):
            chunk = AudioChunk.from_event(event)
            self._audio_chunks.append(chunk.audio)
            return True

        if AudioStop.is_type(event.type):
            await self._finish_transcription()
            return True

        return True

    async def _finish_transcription(self) -> None:
        """Send accumulated audio to OpenRouter and write back a Transcript."""
        pcm_bytes = b"".join(self._audio_chunks)
        self._audio_chunks = []

        if not pcm_bytes:
            await self.write_event(Transcript(text="").event())
            return

        wav_bytes = _build_wav(
            pcm_bytes, self._audio_rate, self._audio_width, self._audio_channels
        )
        audio_seconds = len(pcm_bytes) / (
            self._audio_width * self._audio_channels * self._audio_rate
        )

        try:
            result = await asyncio.to_thread(
                openrouter.transcribe,
                self.task.api_key,
                self.task.model,
                wav_bytes,
                self._requested_language,
                self.task.temperature,
                self.task.provider,
                self.task.timeout,
            )
        except Exception:
            # Log and still return a clean (if unhelpful) response rather than
            # dropping the connection -- a single transient OpenRouter failure
            # shouldn't surface as a hard ERROR result in Home Assistant.
            self._logger.exception("OpenRouter transcription request failed")
            await self.write_event(Transcript(text="").event())
            return

        await self.write_event(
            Transcript(text=result.text, language=self._requested_language).event()
        )

        self._logger.info(
            "Transcribe: model=%s language=%s audio=%.2fs latency=%dms cost=$%.6f",
            self.task.model,
            self._requested_language or "auto",
            audio_seconds,
            result.elapsed_ms,
            result.cost,
        )
        # Debug-only, separate from the line above: transcribed speech content
        # is more sensitive than request metadata, so it's gated behind
        # --debug rather than always logged. usage is the raw dict from
        # OpenRouter and varies by model (seconds vs. input/output/total
        # tokens) -- logged as-is rather than picking fields, so nothing is
        # silently dropped when a new model/provider returns something new.
        self._logger.debug("Transcript text=%r usage=%s", result.text, result.usage)
        self.metrics.record(result.elapsed_ms, result.cost)
        await push_to_supervisor(self.metrics)
