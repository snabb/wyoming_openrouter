"""Thin client for OpenRouter's speech-to-text (transcription) API."""

import base64
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

import requests

_LOGGER = logging.getLogger(__name__)

TRANSCRIPTIONS_URL = "https://openrouter.ai/api/v1/audio/transcriptions"
MODELS_URL = "https://openrouter.ai/api/v1/models"

# Retried once after a short fixed delay; anything else (or a second failure)
# is raised immediately. Full exponential backoff (as used for batch/background
# API calls) would add unacceptable dead air to a live voice interaction.
_RETRYABLE_STATUS = frozenset({429, 502, 503, 504})
_RETRY_DELAY_SECONDS = 0.5


class OpenRouterError(Exception):
    """Raised when a transcription request fails after the retry."""


@dataclass
class TranscriptionResult:
    """Result of a single transcription request."""

    text: str
    cost: float
    elapsed_ms: int
    usage: dict[str, Any]


def transcribe(
    api_key: str,
    model: str,
    wav_bytes: bytes,
    language: Optional[str] = None,
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
            f"OpenRouter transcription request failed: {exc}"
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
