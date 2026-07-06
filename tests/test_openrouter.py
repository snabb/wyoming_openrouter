"""Tests for wyoming_openrouter.openrouter."""

import base64
from unittest.mock import MagicMock, patch

import pytest
import requests

from wyoming_openrouter.openrouter import (
    OpenRouterError,
    TranscriptionResult,
    list_stt_models,
    transcribe,
)


def _fake_response(status_code=200, json_data=None):
    response = MagicMock(spec=requests.Response)
    response.status_code = status_code
    response.json.return_value = json_data or {}
    if status_code >= 400:
        response.raise_for_status.side_effect = requests.HTTPError(f"{status_code} error")
    else:
        response.raise_for_status.return_value = None
    return response


def test_transcribe_sends_expected_request_shape():
    response = _fake_response(json_data={"text": "hello world", "usage": {"cost": 0.0001}})
    with patch(
        "wyoming_openrouter.openrouter.requests.post", return_value=response
    ) as mock_post:
        result = transcribe(
            "sk-test", "openai/whisper-1", b"RIFF....", language="en", timeout=30
        )

    assert mock_post.call_count == 1
    _, kwargs = mock_post.call_args
    assert kwargs["headers"]["Authorization"] == "Bearer sk-test"
    assert kwargs["json"]["model"] == "openai/whisper-1"
    assert kwargs["json"]["input_audio"]["format"] == "wav"
    assert base64.b64decode(kwargs["json"]["input_audio"]["data"]) == b"RIFF...."
    assert kwargs["json"]["language"] == "en"
    assert kwargs["timeout"] == 30
    assert isinstance(result, TranscriptionResult)
    assert result.text == "hello world"
    assert result.cost == 0.0001


def test_transcribe_omits_language_when_not_given():
    response = _fake_response(json_data={"text": "hi", "usage": {"cost": 0}})
    with patch(
        "wyoming_openrouter.openrouter.requests.post", return_value=response
    ) as mock_post:
        transcribe("sk-test", "openai/whisper-1", b"data")

    _, kwargs = mock_post.call_args
    assert "language" not in kwargs["json"]


def test_transcribe_parses_token_based_usage_cost():
    """gpt-4o-mini-transcribe returns token counts, not seconds -- only usage.cost matters."""
    response = _fake_response(
        json_data={
            "text": "",
            "usage": {
                "total_tokens": 22,
                "input_tokens": 20,
                "output_tokens": 2,
                "cost": 3.5e-05,
            },
        }
    )
    with patch("wyoming_openrouter.openrouter.requests.post", return_value=response):
        result = transcribe("sk-test", "openai/gpt-4o-mini-transcribe", b"data")

    assert result.cost == 3.5e-05


def test_transcribe_retries_once_on_retryable_status_then_succeeds():
    retryable = _fake_response(status_code=503)
    success = _fake_response(json_data={"text": "ok", "usage": {"cost": 0.001}})
    with (
        patch(
            "wyoming_openrouter.openrouter.requests.post",
            side_effect=[retryable, success],
        ) as mock_post,
        patch("wyoming_openrouter.openrouter.time.sleep") as mock_sleep,
    ):
        result = transcribe("sk-test", "openai/whisper-1", b"data")

    assert mock_post.call_count == 2
    assert mock_sleep.call_count == 1
    assert result.text == "ok"


def test_transcribe_raises_after_retry_still_fails():
    retryable = _fake_response(status_code=429)
    still_failing = _fake_response(status_code=429)
    with (
        patch(
            "wyoming_openrouter.openrouter.requests.post",
            side_effect=[retryable, still_failing],
        ) as mock_post,
        patch("wyoming_openrouter.openrouter.time.sleep") as mock_sleep,
    ):
        with pytest.raises(OpenRouterError):
            transcribe("sk-test", "openai/whisper-1", b"data")

    assert mock_post.call_count == 2
    assert mock_sleep.call_count == 1


def test_transcribe_nonretryable_4xx_raises_immediately():
    bad_request = _fake_response(status_code=400)
    with patch(
        "wyoming_openrouter.openrouter.requests.post", return_value=bad_request
    ) as mock_post:
        with pytest.raises(OpenRouterError):
            transcribe("sk-test", "openai/whisper-1", b"data")

    assert mock_post.call_count == 1


def test_list_stt_models_returns_data_list():
    response = _fake_response(json_data={"data": [{"id": "openai/whisper-1"}]})
    with patch("wyoming_openrouter.openrouter.requests.get", return_value=response):
        models = list_stt_models("sk-test")

    assert models == [{"id": "openai/whisper-1"}]
