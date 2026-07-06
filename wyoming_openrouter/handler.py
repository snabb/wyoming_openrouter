"""Wyoming event handler for OpenRouter speech-to-text."""

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
from .ha_metrics import Metrics, push_to_supervisor

_LOGGER = logging.getLogger(__name__)

_ATTRIBUTION = Attribution(name="OpenRouter", url="https://openrouter.ai")


def get_wyoming_info(models: list[str], languages: list[str]) -> Info:
    """Create Wyoming info describing the configured OpenRouter STT models.

    Each configured model slug becomes one AsrModel under a single AsrProgram
    (Home Assistant's own wyoming integration only ever looks at info.asr[0]
    and unions all installed AsrModel.languages into supported_languages, so
    there is no benefit to multiple AsrPrograms here).
    """
    asr_models = [
        AsrModel(
            name=model,
            attribution=_ATTRIBUTION,
            installed=True,
            description=f"OpenRouter STT model: {model}",
            version=None,
            languages=list(languages),
        )
        for model in models
    ]

    return Info(
        asr=[
            AsrProgram(
                name="openrouter",
                attribution=_ATTRIBUTION,
                installed=True,
                description="OpenRouter speech-to-text",
                version=__version__,
                models=asr_models,
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


class OpenRouterEventHandler(AsyncEventHandler):
    """Handle Wyoming ASR events by proxying accumulated audio to OpenRouter."""

    def __init__(
        self,
        wyoming_info: Info,
        cli_args,
        metrics: Metrics,
        *args,
        **kwargs,
    ) -> None:
        """Initialize handler."""
        super().__init__(*args, **kwargs)
        self.wyoming_info = wyoming_info
        self.cli_args = cli_args
        self.metrics = metrics

        self._audio_chunks: list[bytes] = []
        self._audio_rate = 16000
        self._audio_width = 2
        self._audio_channels = 1
        self._requested_language: Optional[str] = None
        self._requested_model: str = cli_args.models[0]

    def _resolve_model(self, transcribe: Transcribe) -> str:
        """Honor Transcribe.name if it names one of the configured models.

        Home Assistant's own wyoming integration never sets this field, so in
        the normal HA flow this always falls back to the first configured
        (default) model -- but any other Wyoming client that does pick a
        model by name is still handled correctly.
        """
        if transcribe.name and transcribe.name in self.cli_args.models:
            return transcribe.name
        return self.cli_args.models[0]

    async def handle_event(self, event: Event) -> bool:
        """Handle Wyoming events."""
        if Describe.is_type(event.type):
            await self.write_event(self.wyoming_info.event())
            _LOGGER.debug("Sent info in response to describe")
            return True

        if Transcribe.is_type(event.type):
            transcribe = Transcribe.from_event(event)
            # Home Assistant always sets .language from the Assist pipeline's
            # STT language; pass it straight through. Only fall back to the
            # configured --default-language hint when a client omits it
            # entirely -- otherwise leave it unset and let the model
            # auto-detect.
            self._requested_language = transcribe.language or self.cli_args.default_language
            self._requested_model = self._resolve_model(transcribe)
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
                self.cli_args.api_key,
                self._requested_model,
                wav_bytes,
                self._requested_language,
                self.cli_args.timeout,
            )
        except Exception:
            # Log and still return a clean (if unhelpful) response rather than
            # dropping the connection -- a single transient OpenRouter failure
            # shouldn't surface as a hard ERROR result in Home Assistant.
            _LOGGER.exception("OpenRouter transcription request failed")
            await self.write_event(Transcript(text="").event())
            return

        await self.write_event(
            Transcript(text=result.text, language=self._requested_language).event()
        )

        _LOGGER.info(
            "Transcribe: model=%s language=%s audio=%.2fs latency=%dms cost=$%.6f",
            self._requested_model,
            self._requested_language or "auto",
            audio_seconds,
            result.elapsed_ms,
            result.cost,
        )
        self.metrics.record(result.elapsed_ms, result.cost)
        await push_to_supervisor(self.metrics)
