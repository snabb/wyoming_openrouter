"""Tests for wyoming_openrouter.openrouter."""

import base64
from unittest.mock import MagicMock, patch

import pytest
import requests

from wyoming_openrouter.openrouter import (
    OpenRouterError,
    SpeechMeta,
    TranscriptionResult,
    build_price_per_char_table,
    describe_stt_price,
    describe_tts_price,
    get_generation_cost,
    list_stt_models,
    list_tts_models,
    synthesize_stream,
    transcribe,
)


def _fake_response(status_code=200, json_data=None):
    response = MagicMock(spec=requests.Response)
    response.status_code = status_code
    response.json.return_value = json_data or {}
    if status_code >= 400:
        response.raise_for_status.side_effect = requests.HTTPError(
            f"{status_code} error"
        )
    else:
        response.raise_for_status.return_value = None
    return response


def test_transcribe_sends_expected_request_shape():
    response = _fake_response(
        json_data={"text": "hello world", "usage": {"cost": 0.0001}}
    )
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


def test_transcribe_includes_temperature_and_provider_when_given():
    response = _fake_response(json_data={"text": "hi", "usage": {"cost": 0}})
    with patch(
        "wyoming_openrouter.openrouter.requests.post", return_value=response
    ) as mock_post:
        transcribe(
            "sk-test",
            "openai/whisper-1",
            b"data",
            temperature=0.2,
            provider={"order": ["openai"]},
        )

    _, kwargs = mock_post.call_args
    assert kwargs["json"]["temperature"] == 0.2
    assert kwargs["json"]["provider"] == {"order": ["openai"]}


def test_list_tts_models_returns_data_list():
    response = _fake_response(json_data={"data": [{"id": "hexgrad/kokoro-82m"}]})
    with patch("wyoming_openrouter.openrouter.requests.get", return_value=response):
        models = list_tts_models("sk-test")

    assert models == [{"id": "hexgrad/kokoro-82m"}]


# --- synthesize_stream (TTS) ------------------------------------------------------


def _fake_stream_response(
    status_code=200,
    content_type="audio/pcm;rate=24000;channels=1",
    chunks=None,
    generation_id="gen-1",
):
    response = MagicMock(spec=requests.Response)
    response.status_code = status_code
    response.headers = {"Content-Type": content_type, "X-Generation-Id": generation_id}
    response.iter_content.return_value = iter(chunks or [b"\x00\x01", b"\x02\x03"])
    if status_code >= 400:
        response.raise_for_status.side_effect = requests.HTTPError(
            f"{status_code} error"
        )
    else:
        response.raise_for_status.return_value = None
    return response


def test_synthesize_stream_sends_expected_request_defaulting_to_pcm():
    response = _fake_stream_response()
    with patch(
        "wyoming_openrouter.openrouter.requests.post", return_value=response
    ) as mock_post:
        meta, chunks = synthesize_stream(
            "sk-test",
            "openai/gpt-4o-mini-tts",
            "hello",
            "alloy",
            speed=1.5,
            provider={"order": ["openai"]},
            timeout=30,
        )
        list(chunks)  # drain the generator

    assert mock_post.call_count == 1
    _, kwargs = mock_post.call_args
    assert kwargs["headers"]["Authorization"] == "Bearer sk-test"
    assert kwargs["json"]["model"] == "openai/gpt-4o-mini-tts"
    assert kwargs["json"]["input"] == "hello"
    assert kwargs["json"]["voice"] == "alloy"
    assert kwargs["json"]["response_format"] == "pcm"
    assert kwargs["json"]["speed"] == 1.5
    assert kwargs["json"]["provider"] == {"order": ["openai"]}
    assert kwargs["stream"] is True


def test_synthesize_stream_requests_mp3_when_given():
    response = _fake_stream_response(content_type="audio/mpeg")
    with patch(
        "wyoming_openrouter.openrouter.requests.post", return_value=response
    ) as mock_post:
        meta, chunks = synthesize_stream(
            "sk-test",
            "mistralai/voxtral-mini-tts-2603",
            "hello",
            "en_paul_neutral",
            response_format="mp3",
        )
        list(chunks)

    _, kwargs = mock_post.call_args
    assert kwargs["json"]["response_format"] == "mp3"
    assert isinstance(meta, SpeechMeta)
    assert meta.rate == 24000
    assert meta.channels == 1
    assert meta.width == 2
    assert meta.generation_id == "gen-1"


def test_synthesize_stream_parses_content_type_rate_and_channels():
    response = _fake_stream_response(content_type="audio/pcm;rate=16000;channels=2")
    with patch("wyoming_openrouter.openrouter.requests.post", return_value=response):
        meta, chunks = synthesize_stream("sk-test", "m", "hi", "alloy")
        list(chunks)

    assert meta.rate == 16000
    assert meta.channels == 2


def test_synthesize_stream_falls_back_to_default_format_when_header_missing():
    response = _fake_stream_response(content_type="")
    with patch("wyoming_openrouter.openrouter.requests.post", return_value=response):
        meta, chunks = synthesize_stream("sk-test", "m", "hi", "alloy")
        list(chunks)

    assert meta.rate == 24000
    assert meta.channels == 1


def test_synthesize_stream_yields_chunks_lazily():
    response = _fake_stream_response(chunks=[b"aaa", b"bbb", b"ccc"])
    with patch("wyoming_openrouter.openrouter.requests.post", return_value=response):
        meta, chunks = synthesize_stream("sk-test", "m", "hi", "alloy")
        assert list(chunks) == [b"aaa", b"bbb", b"ccc"]
    response.close.assert_called_once()


def test_synthesize_stream_retries_once_on_retryable_status_then_succeeds():
    retryable = _fake_stream_response(status_code=503)
    success = _fake_stream_response()
    with (
        patch(
            "wyoming_openrouter.openrouter.requests.post",
            side_effect=[retryable, success],
        ) as mock_post,
        patch("wyoming_openrouter.openrouter.time.sleep") as mock_sleep,
    ):
        meta, chunks = synthesize_stream("sk-test", "m", "hi", "alloy")
        list(chunks)

    assert mock_post.call_count == 2
    assert mock_sleep.call_count == 1
    retryable.close.assert_called_once()


def test_synthesize_stream_nonretryable_error_raises_and_closes_response():
    bad_request = _fake_stream_response(status_code=400)
    with patch("wyoming_openrouter.openrouter.requests.post", return_value=bad_request):
        with pytest.raises(OpenRouterError):
            synthesize_stream("sk-test", "m", "hi", "alloy")

    bad_request.close.assert_called_once()


# --- get_generation_cost -----------------------------------------------------------


def test_get_generation_cost_returns_value_when_resolved():
    response = _fake_response(json_data={"data": {"total_cost": 0.00042}})
    with patch("wyoming_openrouter.openrouter.requests.get", return_value=response):
        cost = get_generation_cost("sk-test", "gen-1")

    assert cost == 0.00042


def test_get_generation_cost_returns_none_when_not_yet_propagated():
    response = _fake_response(json_data={"data": {"total_cost": None}})
    with patch("wyoming_openrouter.openrouter.requests.get", return_value=response):
        cost = get_generation_cost("sk-test", "gen-1")

    assert cost is None


def test_get_generation_cost_returns_none_on_network_failure_never_raises():
    with patch(
        "wyoming_openrouter.openrouter.requests.get",
        side_effect=requests.ConnectionError(),
    ):
        cost = get_generation_cost("sk-test", "gen-1")

    assert cost is None


# --- build_price_per_char_table -----------------------------------------------------


def test_build_price_per_char_table_includes_zero_completion_models():
    catalog = [
        {
            "id": "hexgrad/kokoro-82m",
            "pricing": {"prompt": "0.00000062", "completion": "0"},
        },
        {
            "id": "mistralai/voxtral-mini-tts-2603",
            "pricing": {"prompt": "0.000016", "completion": 0},
        },
    ]
    table = build_price_per_char_table(catalog)
    assert table == {
        "hexgrad/kokoro-82m": 0.00000062,
        "mistralai/voxtral-mini-tts-2603": 0.000016,
    }


def test_build_price_per_char_table_excludes_nonzero_completion_outlier():
    catalog = [
        {
            "id": "google/gemini-3.1-flash-tts-preview",
            "pricing": {"prompt": "0.000001", "completion": "0.00002"},
        },
    ]
    table = build_price_per_char_table(catalog)
    assert table == {}


def test_build_price_per_char_table_skips_malformed_entries():
    catalog = [
        {"id": "no-pricing-field"},
        {"id": "bad-prompt", "pricing": {"prompt": "not-a-number", "completion": "0"}},
    ]
    table = build_price_per_char_table(catalog)
    assert table == {}


# --- describe_stt_price / describe_tts_price --------------------------------------


def test_describe_stt_price_labels_duration_priced_model():
    model = {
        "id": "openai/whisper-1",
        "pricing": {"prompt": "0.006", "completion": "0"},
    }
    assert describe_stt_price(model) == "$0.006/duration-unit"


def test_describe_stt_price_labels_per_token_model():
    model = {
        "id": "openai/gpt-4o-mini-transcribe",
        "pricing": {"prompt": "0.00000125", "completion": "0.000005"},
    }
    assert (
        describe_stt_price(model) == "$0.00000125/input-token, $0.000005/output-token"
    )


def test_describe_tts_price_labels_ordinary_model_per_char():
    model = {
        "id": "hexgrad/kokoro-82m",
        "pricing": {"prompt": "0.00000062", "completion": "0"},
    }
    assert describe_tts_price(model) == "$0.00000062/char"


def test_describe_tts_price_flags_nonzero_completion_outlier():
    model = {
        "id": "google/gemini-3.1-flash-tts-preview",
        "pricing": {"prompt": "0.000001", "completion": "0.00002"},
    }
    assert (
        describe_tts_price(model)
        == "$0.000001/char (approx -- priced per output audio token, not input character)"
    )
