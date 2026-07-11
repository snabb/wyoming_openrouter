"""Wyoming event handler for one OpenRouter text-to-speech task."""

import asyncio
import logging
import threading
import time
from typing import AsyncIterator, Iterator, Optional

from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.event import Event
from wyoming.info import Attribution, Describe, Info, TtsProgram, TtsVoice
from wyoming.server import AsyncEventHandler
from wyoming.tts import Synthesize, SynthesizeStopped

from . import __version__, mp3_decode, openrouter
from .config import TaskConfig
from .ha_metrics import Metrics, push_to_supervisor

_ATTRIBUTION = Attribution(name="OpenRouter", url="https://openrouter.ai")

# Bounded retry budget for the async generation-cost lookup: OpenRouter's
# generation record has a real, observed seconds-scale propagation delay
# (confirmed empirically -- querying immediately after a request returns null
# fields; ~2s later it's fully populated) -- these delays comfortably clear
# that while capping the total wait to ~15s.
_COST_LOOKUP_DELAYS = (1.0, 2.0, 4.0, 8.0)

# Used only for the placeholder AudioStart/AudioStop pair on an empty-text or
# pre-AudioStart failure path, where no real format was ever negotiated.
_FALLBACK_RATE = 24000
_FALLBACK_WIDTH = 2
_FALLBACK_CHANNELS = 1


def get_tts_wyoming_info(task: TaskConfig) -> Info:
    """Create Wyoming info describing this task's single dedicated TTS model/voice."""
    # HA's WyomingTtsProvider only sets _attr_default_language when
    # _attr_supported_languages is non-empty (derived from voice.languages)
    # -- otherwise it never sets the attribute at all, and HA's tts entity
    # base class crashes with AttributeError the moment it's added
    # (accessing the default_language cached property), taking the whole
    # entity down. Confirmed live: an empty languages list here doesn't just
    # leave the entity unselectable (like the analogous stt case), it
    # prevents the entity from being created at all. Config validation defaults
    # TTS tasks to English, so this list can never be empty.
    return Info(
        tts=[
            TtsProgram(
                # HA's wyoming integration uses this as both the entity's
                # display name (WyomingTtsProvider._attr_name) and the
                # config-entry title when added manually (WyomingService.
                # get_name()) -- must include the task name, or every task
                # looks identical ("openrouter") in HA.
                name=f"OpenRouter ({task.name})",
                attribution=_ATTRIBUTION,
                installed=True,
                description=f"OpenRouter text-to-speech: {task.name}",
                version=__version__,
                voices=[
                    TtsVoice(
                        name=task.voice or "default",
                        attribution=_ATTRIBUTION,
                        installed=True,
                        description=f"OpenRouter TTS voice: {task.voice}",
                        version=None,
                        languages=task.languages,
                    )
                ],
                supports_synthesize_streaming=True,
            )
        ]
    )


async def _bridge_to_async(sync_iter: Iterator[bytes]) -> AsyncIterator[bytes]:
    """Read a blocking byte-chunk generator on a worker thread, yielding each
    chunk to the event loop as soon as it arrives rather than blocking the
    whole loop on the underlying (blocking) iter_content() call."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    done = object()

    def _pump() -> None:
        try:
            for chunk in sync_iter:
                loop.call_soon_threadsafe(queue.put_nowait, chunk)
        except Exception as exc:  # forwarded to the async consumer below
            loop.call_soon_threadsafe(queue.put_nowait, exc)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, done)

    threading.Thread(target=_pump, daemon=True).start()

    while True:
        item = await queue.get()
        if item is done:
            return
        if isinstance(item, Exception):
            raise item
        yield item


class OpenRouterTtsEventHandler(AsyncEventHandler):
    """Handle Wyoming TTS events for one task by proxying text to OpenRouter."""

    def __init__(
        self,
        wyoming_info: Info,
        task: TaskConfig,
        metrics: Metrics,
        tts_pricing: dict[str, float],
        *args,
        **kwargs,
    ) -> None:
        """Initialize handler."""
        super().__init__(*args, **kwargs)
        self.wyoming_info = wyoming_info
        self.task = task
        self.metrics = metrics
        self.tts_pricing = tts_pricing
        self._logger = logging.getLogger(f"wyoming_openrouter.task.{task.slug}")
        # Fire-and-forget cost-resolution tasks must be kept referenced
        # somewhere, or asyncio may garbage-collect them mid-flight.
        self._background_tasks: set[asyncio.Task] = set()

    async def handle_event(self, event: Event) -> bool:
        """Handle Wyoming events."""
        if Describe.is_type(event.type):
            await self.write_event(self.wyoming_info.event())
            self._logger.debug("Sent info in response to describe")
            return True

        if Synthesize.is_type(event.type):
            await self._handle_synthesize(Synthesize.from_event(event))
            return True

        # SynthesizeStart/Chunk/Stop (streaming TEXT input) are safely
        # ignored: Home Assistant's own client always also sends one full
        # classic Synthesize event with the complete accumulated text "for
        # backwards compatibility" alongside these, so acting only on that
        # event is sufficient (same reasoning as wyoming_bluetts).
        return True

    async def _handle_synthesize(self, synthesize: Synthesize) -> None:
        text = (synthesize.text or "").strip()
        voice = self.task.voice
        if synthesize.voice and synthesize.voice.name:
            voice = synthesize.voice.name
        # config.py requires a non-empty voice for every tts-type task, so
        # this handler is never constructed for a task where this could
        # actually be None -- asserted (rather than silently trusted) so a
        # future config-validation gap would fail loudly here instead of
        # sending a confusing voice=None request to OpenRouter.
        assert voice is not None, "tts task must have a configured voice"

        self._logger.info(
            "Synthesize request: model=%s voice=%s chars=%d",
            self.task.model,
            voice,
            len(text),
        )

        chunk_count = 0
        audio_bytes = 0
        generation_id: Optional[str] = None
        speech_response_received = False
        audio_start_sent = False
        t_start = time.monotonic()

        try:
            if text:
                meta, sync_iter = await asyncio.to_thread(
                    openrouter.synthesize_stream,
                    self.task.api_key,
                    self.task.model,
                    text,
                    voice,
                    self.task.speed,
                    self.task.provider,
                    self.task.timeout,
                    self.task.audio_format,
                )
                speech_response_received = True
                generation_id = meta.generation_id
                if self.task.audio_format == "mp3":
                    # meta.rate/width/channels describe OpenRouter's own pcm
                    # Content-Type header and are meaningless for an mp3
                    # response -- mpg123 is forced to a fixed known output
                    # format instead, decoded before Wyoming ever sees it.
                    rate = mp3_decode.DECODED_RATE
                    width = mp3_decode.DECODED_WIDTH
                    channels = mp3_decode.DECODED_CHANNELS
                    sync_iter = mp3_decode.decode_mp3_stream(sync_iter)
                else:
                    rate, width, channels = meta.rate, meta.width, meta.channels
                await self.write_event(
                    AudioStart(rate=rate, width=width, channels=channels).event()
                )
                audio_start_sent = True
                async for chunk in _bridge_to_async(sync_iter):
                    await self.write_event(
                        AudioChunk(
                            audio=chunk,
                            rate=rate,
                            width=width,
                            channels=channels,
                        ).event()
                    )
                    chunk_count += 1
                    audio_bytes += len(chunk)
        except Exception:
            self._logger.exception("OpenRouter speech request failed")
        finally:
            if not audio_start_sent:
                await self.write_event(
                    AudioStart(
                        rate=_FALLBACK_RATE,
                        width=_FALLBACK_WIDTH,
                        channels=_FALLBACK_CHANNELS,
                    ).event()
                )
            await self.write_event(AudioStop().event())
            # Required whenever we advertise supports_synthesize_streaming:
            # Home Assistant's incremental streaming client reads events
            # until SynthesizeStopped specifically (it never checks for
            # AudioStop), so omitting this would hang it forever -- even on
            # the exception path above.
            await self.write_event(SynthesizeStopped().event())

        elapsed_ms = int((time.monotonic() - t_start) * 1000)
        self._logger.info(
            "Synthesized %d byte(s) of audio in %d chunk(s), %d ms",
            audio_bytes,
            chunk_count,
            elapsed_ms,
        )

        if speech_response_received:
            task = asyncio.create_task(
                self._resolve_cost(generation_id, text, elapsed_ms)
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

    async def _resolve_cost(
        self, generation_id: Optional[str], text: str, elapsed_ms: int
    ) -> None:
        """Fire-and-forget: resolve real cost, then log + record metrics.

        Runs entirely after AudioStop/SynthesizeStopped have already been
        sent, so a slow or failed cost lookup never delays audio delivery.
        """
        cost: Optional[float] = None
        if generation_id:
            for delay in _COST_LOOKUP_DELAYS:
                await asyncio.sleep(delay)
                cost = await asyncio.to_thread(
                    openrouter.get_generation_cost,
                    self.task.api_key,
                    generation_id,
                    self.task.timeout,
                )
                if cost is not None:
                    break

        estimated = False
        if cost is None:
            price_per_char = self.tts_pricing.get(self.task.model)
            if price_per_char is not None:
                cost = price_per_char * len(text)
                estimated = True

        if cost is not None:
            self._logger.info(
                "Synthesize cost: model=%s cost=$%.6f%s (generation_id=%s)",
                self.task.model,
                cost,
                " (estimated)" if estimated else "",
                generation_id,
            )
        else:
            self._logger.warning(
                "Could not resolve or estimate synthesis cost%s; recording as unknown",
                f" for generation_id={generation_id}" if generation_id else "",
            )

        self.metrics.record(elapsed_ms, cost)
        await push_to_supervisor(self.metrics)
