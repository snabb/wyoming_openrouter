"""Documented speech-model languages used by configuration scripts.

OpenRouter's model catalog does not expose structured language metadata. Keep
this table explicit so catalog additions are noticed and reviewed instead of
being assigned capabilities by parsing free-form descriptions.
"""

from __future__ import annotations

WHISPER_LANGUAGES = (
    "af", "am", "ar", "as", "az", "ba", "be", "bg", "bn", "bo", "br",
    "bs", "ca", "cs", "cy", "da", "de", "el", "en", "es", "et", "eu",
    "fa", "fi", "fo", "fr", "gl", "gu", "ha", "haw", "he", "hi", "hr",
    "ht", "hu", "hy", "id", "is", "it", "ja", "jw", "ka", "kk", "km",
    "kn", "ko", "la", "lb", "ln", "lo", "lt", "lv", "mg", "mi", "mk",
    "ml", "mn", "mr", "ms", "mt", "my", "ne", "nl", "nn", "no", "oc",
    "pa", "pl", "ps", "pt", "ro", "ru", "sa", "sd", "si", "sk", "sl",
    "sn", "so", "sq", "sr", "su", "sv", "sw", "ta", "te", "tg", "th",
    "tk", "tl", "tr", "tt", "uk", "ur", "uz", "vi", "yi", "yo", "zh",
)

EUROPEAN_25 = (
    "bg", "hr", "cs", "da", "nl", "en", "et", "fi", "fr", "de", "el",
    "hu", "it", "lv", "lt", "mt", "pl", "pt", "ro", "ru", "sk", "sl",
    "es", "sv", "uk",
)

QWEN_ASR_LANGUAGES = (
    "zh", "en", "yue", "fr", "de", "it", "es", "pt", "ja", "ko", "ru",
)

VOXTRAL_TRANSCRIBE_LANGUAGES = (
    "en", "fr", "de", "es", "it", "pt", "nl", "hi", "ar", "zh", "ja",
    "ko", "ru",
)

STT_MODEL_LANGUAGES: dict[str, tuple[str, ...]] = {
    "microsoft/mai-transcribe-1.5": WHISPER_LANGUAGES,
    "nvidia/parakeet-tdt-0.6b-v3": EUROPEAN_25,
    "mistralai/voxtral-mini-transcribe": VOXTRAL_TRANSCRIBE_LANGUAGES,
    "qwen/qwen3-asr-flash-2026-02-10": QWEN_ASR_LANGUAGES,
    "google/chirp-3": WHISPER_LANGUAGES,
    "openai/gpt-4o-mini-transcribe": WHISPER_LANGUAGES,
    "openai/whisper-large-v3-turbo": WHISPER_LANGUAGES,
    "openai/whisper-large-v3": WHISPER_LANGUAGES,
    "openai/whisper-1": WHISPER_LANGUAGES,
    "openai/gpt-4o-transcribe": WHISPER_LANGUAGES,
}

GROK_TTS_LANGUAGES = (
    "ar", "bn", "zh", "en", "fr", "de", "hi", "id", "it", "ja", "ko",
    "pt", "ru", "es", "tr", "vi",
)

GEMINI_TTS_LANGUAGES = (
    "ar", "bn", "nl", "en", "fr", "de", "hi", "id", "it", "ja", "ko",
    "mr", "pl", "pt", "ro", "ru", "es", "ta", "te", "th", "tr", "uk",
    "vi",
)

TTS_MODEL_LANGUAGES: dict[str, tuple[str, ...]] = {
    "x-ai/grok-voice-tts-1.0": GROK_TTS_LANGUAGES,
    "google/gemini-3.1-flash-tts-preview": GEMINI_TTS_LANGUAGES,
    "zyphra/zonos-v0.1-transformer": ("en",),
    "zyphra/zonos-v0.1-hybrid": ("en",),
    "sesame/csm-1b": ("en",),
    "canopylabs/orpheus-3b-0.1-ft": ("en",),
}

KOKORO_PREFIX_LANGUAGES = {
    "a": "en",  # American English
    "b": "en",  # British English
    "e": "es",
    "f": "fr",
    "h": "hi",
    "i": "it",
    "j": "ja",
    "p": "pt",
    "z": "zh",
}


def stt_languages(model_id: str) -> tuple[str, ...] | None:
    """Return documented STT languages, or None for an unknown model."""
    return STT_MODEL_LANGUAGES.get(model_id)


def tts_languages(model_id: str, voice: str) -> tuple[str, ...] | None:
    """Return languages supported by a selected TTS voice."""
    if model_id == "microsoft/mai-voice-2":
        locale = voice.split(":", 1)[0]
        if "-" in locale:
            return (locale.split("-", 1)[0].lower(),)
        return None

    if model_id == "hexgrad/kokoro-82m":
        language = KOKORO_PREFIX_LANGUAGES.get(voice[:1].lower())
        return (language,) if language else None

    if model_id == "mistralai/voxtral-mini-tts-2603":
        prefix = voice.split("_", 1)[0].lower()
        if prefix in {"en", "gb"}:
            return ("en",)
        if prefix == "fr":
            return ("fr",)
        return None

    return TTS_MODEL_LANGUAGES.get(model_id)
