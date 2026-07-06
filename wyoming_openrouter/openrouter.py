"""Thin client for OpenRouter's speech-to-text and text-to-speech APIs."""

import base64
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Iterator, Optional

import requests

_LOGGER = logging.getLogger(__name__)

TRANSCRIPTIONS_URL = "https://openrouter.ai/api/v1/audio/transcriptions"
SPEECH_URL = "https://openrouter.ai/api/v1/audio/speech"
GENERATION_URL = "https://openrouter.ai/api/v1/generation"
MODELS_URL = "https://openrouter.ai/api/v1/models"

_SPEECH_CONTENT_TYPE_RE = re.compile(r"rate=(\d+);channels=(\d+)")
_SPEECH_CHUNK_BYTES = 4096
# OpenRouter's pcm Content-Type states rate/channels but never bit depth;
# 16-bit signed little-endian is the universal assumption Wyoming itself makes
# elsewhere, and matches the byte/duration arithmetic on a real test response.
_SPEECH_SAMPLE_WIDTH = 2

# Retried once after a short fixed delay; anything else (or a second failure)
# is raised immediately. Full exponential backoff (as used for batch/background
# API calls) would add unacceptable dead air to a live voice interaction.
_RETRYABLE_STATUS = frozenset({429, 502, 503, 504})
_RETRY_DELAY_SECONDS = 0.5


class OpenRouterError(Exception):
    """Raised when a transcription or speech request fails after the retry."""


def _extract_error_message(response: requests.Response) -> str:
    """Pull OpenRouter's own {"error": {"message": ...}} out of a failed
    response, falling back to raw body text -- the generic HTTPError string
    alone (e.g. "400 Client Error: Bad Request for url: ...") hides the
    actually useful detail, such as a specific model rejecting a requested
    parameter value.
    """
    try:
        data = response.json()
        message = data.get("error", {}).get("message")
        if message:
            return str(message)
    except Exception:
        pass
    return response.text[:500] if response.text else str(response.status_code)


@dataclass
class TranscriptionResult:
    """Result of a single transcription request."""

    text: str
    cost: float
    elapsed_ms: int
    usage: dict[str, Any]


@dataclass
class SpeechMeta:
    """Audio format + tracking info known as soon as a speech response's headers arrive."""

    rate: int
    width: int
    channels: int
    generation_id: Optional[str]


def transcribe(
    api_key: str,
    model: str,
    wav_bytes: bytes,
    language: Optional[str] = None,
    temperature: Optional[float] = None,
    provider: Optional[dict[str, Any]] = None,
    timeout: float = 60.0,
) -> TranscriptionResult:
    """Transcribe a WAV clip via OpenRouter's audio/transcriptions endpoint.

    Audio is sent as base64-encoded raw bytes (not a data URI) inside a JSON
    body, per OpenRouter's documented STT request shape -- unlike OpenAI's own
    multipart/form-data Whisper endpoint of the same name.
    """
    body: dict[str, Any] = {
        "model": model,
        "input_audio": {
            "data": base64.b64encode(wav_bytes).decode("ascii"),
            "format": "wav",
        },
    }
    if language:
        body["language"] = language
    if temperature is not None:
        body["temperature"] = temperature
    if provider:
        body["provider"] = provider

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    t_start = time.monotonic()
    response = requests.post(
        TRANSCRIPTIONS_URL, headers=headers, json=body, timeout=timeout
    )
    if response.status_code in _RETRYABLE_STATUS:
        _LOGGER.warning(
            "OpenRouter transcription request failed with HTTP %d, retrying once",
            response.status_code,
        )
        time.sleep(_RETRY_DELAY_SECONDS)
        response = requests.post(
            TRANSCRIPTIONS_URL, headers=headers, json=body, timeout=timeout
        )

    elapsed_ms = int((time.monotonic() - t_start) * 1000)

    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise OpenRouterError(
            f"OpenRouter transcription request failed: {_extract_error_message(response)}"
        ) from exc

    payload = response.json()
    usage = payload.get("usage") or {}
    cost = float(usage.get("cost") or 0)

    return TranscriptionResult(
        text=payload.get("text", ""),
        cost=cost,
        elapsed_ms=elapsed_ms,
        usage=usage,
    )


def list_stt_models(api_key: Optional[str] = None, timeout: float = 10.0) -> list[dict[str, Any]]:
    """Best-effort fetch of the live OpenRouter STT model catalog for the startup log.

    Never raises: callers should log-and-continue on failure rather than block
    startup on a catalog fetch that isn't required for the server to function.
    """
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    response = requests.get(
        MODELS_URL,
        params={"output_modalities": "transcription"},
        headers=headers,
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json().get("data", [])


def list_tts_models(api_key: Optional[str] = None, timeout: float = 10.0) -> list[dict[str, Any]]:
    """Best-effort fetch of the live OpenRouter TTS model catalog for the startup log."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    response = requests.get(
        MODELS_URL,
        params={"output_modalities": "speech"},
        headers=headers,
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json().get("data", [])


def _parse_speech_content_type(content_type: str) -> tuple[int, int]:
    match = _SPEECH_CONTENT_TYPE_RE.search(content_type or "")
    if not match:
        # OpenRouter's own documented pcm default (24kHz mono) if the header
        # is ever missing the parameters for some reason.
        return 24000, 1
    return int(match.group(1)), int(match.group(2))


def synthesize_stream(
    api_key: str,
    model: str,
    text: str,
    voice: str,
    speed: float = 1.0,
    provider: Optional[dict[str, Any]] = None,
    timeout: float = 60.0,
    response_format: str = "pcm",
) -> tuple[SpeechMeta, Iterator[bytes]]:
    """Start a streaming OpenRouter text-to-speech request.

    Wyoming AudioChunk delivery always needs raw PCM, never a compressed
    container -- but not every OpenRouter TTS model supports
    response_format="pcm" (some, e.g. Mistral's voxtral-mini-tts, only offer
    mp3). Callers that request "mp3" are responsible for decoding the
    returned bytes to PCM themselves (see wyoming_openrouter.mp3_decode)
    before treating this function's SpeechMeta.rate/width/channels as
    accurate -- those fields describe OpenRouter's own pcm Content-Type
    header and are meaningless for an mp3 response (which carries no
    rate/channel info in its Content-Type).

    Returns audio-format metadata as soon as the response headers arrive,
    plus a blocking generator of byte chunks read incrementally as
    OpenRouter's backend produces them (rather than buffering the whole clip
    before returning).
    """
    body: dict[str, Any] = {
        "model": model,
        "input": text,
        "voice": voice,
        "response_format": response_format,
        "speed": speed,
    }
    if provider:
        body["provider"] = provider

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    response = requests.post(
        SPEECH_URL, headers=headers, json=body, timeout=timeout, stream=True
    )
    if response.status_code in _RETRYABLE_STATUS:
        _LOGGER.warning(
            "OpenRouter speech request failed with HTTP %d, retrying once",
            response.status_code,
        )
        response.close()
        time.sleep(_RETRY_DELAY_SECONDS)
        response = requests.post(
            SPEECH_URL, headers=headers, json=body, timeout=timeout, stream=True
        )

    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        message = _extract_error_message(response)
        response.close()
        raise OpenRouterError(f"OpenRouter speech request failed: {message}") from exc

    rate, channels = _parse_speech_content_type(response.headers.get("Content-Type", ""))
    meta = SpeechMeta(
        rate=rate,
        width=_SPEECH_SAMPLE_WIDTH,
        channels=channels,
        generation_id=response.headers.get("X-Generation-Id"),
    )

    def _iter_chunks() -> Iterator[bytes]:
        try:
            for chunk in response.iter_content(chunk_size=_SPEECH_CHUNK_BYTES):
                if chunk:
                    yield chunk
        finally:
            response.close()

    return meta, _iter_chunks()


def build_price_per_char_table(catalog: list[dict[str, Any]]) -> dict[str, float]:
    """From a TTS model catalog (list_tts_models()'s return value), extract a
    model_id -> USD-per-character table, for models confirmed to be priced
    that way.

    Verified live: a real generation's total_cost exactly equals
    pricing.prompt * character-count for ordinary TTS models. Models with a
    nonzero pricing.completion (e.g. google/gemini-3.1-flash-tts-preview) are
    priced differently (likely by output audio tokens, not input characters)
    and are excluded here -- using this formula for them would silently
    undercount cost, so callers should treat those (and any model missing
    from the catalog) as having no safe local estimate.
    """
    table: dict[str, float] = {}
    for model in catalog:
        pricing = model.get("pricing") or {}
        try:
            completion = float(pricing.get("completion") or 0)
        except (TypeError, ValueError):
            continue
        if completion != 0:
            continue
        try:
            table[model["id"]] = float(pricing["prompt"])
        except (KeyError, TypeError, ValueError):
            continue
    return table


def get_generation_cost(
    api_key: str, generation_id: str, timeout: float = 10.0
) -> Optional[float]:
    """Best-effort lookup of a generation's real cost from OpenRouter.

    Never raises -- returns None both on request failure and when the
    generation record hasn't propagated yet (there's a real, observed
    seconds-scale delay between a speech response completing and its cost
    becoming queryable here). Callers should retry with backoff and treat
    None as "not resolved yet", not as an error.
    """
    try:
        response = requests.get(
            GENERATION_URL,
            params={"id": generation_id},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )
        response.raise_for_status()
        cost = response.json().get("data", {}).get("total_cost")
        return float(cost) if cost is not None else None
    except Exception:
        return None
