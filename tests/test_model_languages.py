"""Tests for the curated OpenRouter speech language map."""

import pytest
from scripts.model_languages import (
    STT_MODEL_LANGUAGES,
    assign_task_ports,
    stt_languages,
    tts_audio_format,
    tts_languages,
)


def test_every_current_stt_model_has_languages():
    assert len(STT_MODEL_LANGUAGES) == 10
    assert all(languages for languages in STT_MODEL_LANGUAGES.values())


def test_stt_unknown_model_returns_none():
    assert stt_languages("vendor/future-model") is None


def test_tts_model_wide_multilingual_voice():
    languages = tts_languages("x-ai/grok-voice-tts-1.0", "eve")
    assert languages is not None
    assert {"en", "de", "fr", "es"} <= set(languages)

    gemini_languages = tts_languages("google/gemini-3.1-flash-tts-preview", "Kore")
    assert gemini_languages is not None
    assert "fi" in gemini_languages


def test_tts_audio_format_uses_gemini_pcm_quirk():
    assert tts_audio_format("google/gemini-3.1-flash-tts-preview") == "pcm"
    assert tts_audio_format("x-ai/grok-voice-tts-1.0") == "mp3"


def test_task_ports_preserve_existing_models_and_fill_free_slots():
    keys = [("tts", "vendor/old"), ("tts", "vendor/new")]
    existing = [{"type": "tts", "model": "vendor/old", "port": 10305}]
    assert assign_task_ports(keys, existing, 10300, 10305) == {
        ("tts", "vendor/old"): 10305,
        ("tts", "vendor/new"): 10300,
    }


def test_task_ports_reject_exhausted_range():
    with pytest.raises(ValueError, match="exceed reserved ports"):
        assign_task_ports(
            [("tts", "vendor/one"), ("tts", "vendor/two")], [], 10300, 10300
        )


def test_tts_locale_and_prefix_specific_voices():
    assert tts_languages("microsoft/mai-voice-2", "de-DE-Klaus:MAI-Voice-2") == ("de",)
    assert tts_languages("hexgrad/kokoro-82m", "ff_siwis") == ("fr",)
    assert tts_languages("hexgrad/kokoro-82m", "af_nova") == ("en",)
    assert tts_languages("mistralai/voxtral-mini-tts-2603", "gb_jane_sad") == ("en",)


def test_tts_unknown_model_or_voice_returns_none():
    assert tts_languages("vendor/future-model", "voice") is None
    assert tts_languages("hexgrad/kokoro-82m", "unknown") is None
