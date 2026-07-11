"""Tests for wyoming_openrouter.config."""

import pytest

from wyoming_openrouter.config import ConfigError, plan_tasks


def _stt_task(**overrides):
    task = {
        "name": "kitchen-stt",
        "api_key": "sk-test",
        "type": "stt",
        "port": 10300,
        "model": "openai/gpt-4o-mini-transcribe",
        "language": "en",
    }
    task.update(overrides)
    return task


def _tts_task(**overrides):
    task = {
        "name": "assist-tts",
        "api_key": "sk-test",
        "type": "tts",
        "port": 10301,
        "model": "openai/gpt-4o-mini-tts",
        "voice": "alloy",
    }
    task.update(overrides)
    return task


def test_valid_minimal_stt_and_tts_config_parses():
    tasks = plan_tasks({"tasks": [_stt_task(), _tts_task()]})

    assert len(tasks) == 2
    stt, tts = tasks
    assert stt.name == "kitchen-stt"
    assert stt.slug == "kitchen_stt"
    assert stt.type == "stt"
    assert stt.timeout == 60.0
    assert tts.voice == "alloy"
    assert tts.speed == 1.0
    assert tts.audio_format == "pcm"


def test_no_tasks_raises():
    with pytest.raises(ConfigError, match="at least one is required"):
        plan_tasks({"tasks": []})
    with pytest.raises(ConfigError, match="at least one is required"):
        plan_tasks({})


def test_no_cap_on_number_of_tasks():
    tasks = [_stt_task(name=f"t{i}", port=10300 + i) for i in range(50)]
    assert len(plan_tasks({"tasks": tasks})) == 50


@pytest.mark.parametrize("field", ["name", "api_key", "type", "port", "model"])
def test_missing_required_field_raises(field):
    task = _stt_task()
    del task[field]
    with pytest.raises(ConfigError, match=f"'{field}' is required"):
        plan_tasks({"tasks": [task]})


def test_invalid_type_raises():
    with pytest.raises(ConfigError, match="'type' must be one of"):
        plan_tasks({"tasks": [_stt_task(type="video")]})


def test_voice_required_for_tts():
    task = _tts_task()
    del task["voice"]
    with pytest.raises(ConfigError, match="'voice' is required for a tts task"):
        plan_tasks({"tasks": [task]})


def test_voice_not_required_for_stt():
    tasks = plan_tasks({"tasks": [_stt_task()]})
    assert tasks[0].voice is None


def test_language_required_for_stt():
    task = _stt_task()
    del task["language"]
    with pytest.raises(ConfigError, match="'language' is required for a stt task"):
        plan_tasks({"tasks": [task]})


def test_language_not_required_for_tts():
    tasks = plan_tasks({"tasks": [_tts_task()]})
    assert tasks[0].language is None


def test_duplicate_ports_raise():
    with pytest.raises(ConfigError, match="both use port 10300"):
        plan_tasks(
            {
                "tasks": [
                    _stt_task(name="a", port=10300),
                    _stt_task(name="b", port=10300),
                ]
            }
        )


def test_duplicate_slugs_raise():
    with pytest.raises(ConfigError, match="both map to slug 'living_room'"):
        plan_tasks(
            {
                "tasks": [
                    _stt_task(name="Living Room", port=10300),
                    _stt_task(name="Living-Room", port=10301),
                ]
            }
        )


def test_privileged_port_raises():
    with pytest.raises(ConfigError, match="must be between"):
        plan_tasks({"tasks": [_stt_task(port=80)]})


def test_ephemeral_range_port_raises():
    with pytest.raises(ConfigError, match="must be between"):
        plan_tasks({"tasks": [_stt_task(port=60000)]})


def test_registered_range_port_accepted():
    tasks = plan_tasks({"tasks": [_stt_task(port=8080)]})
    assert tasks[0].port == 8080


def test_invalid_provider_json_raises():
    with pytest.raises(ConfigError, match="'provider' is not valid JSON"):
        plan_tasks({"tasks": [_stt_task(provider="{not json")]})


def test_valid_provider_json_parsed():
    tasks = plan_tasks({"tasks": [_stt_task(provider='{"order": ["openai"]}')]})
    assert tasks[0].provider == {"order": ["openai"]}


def test_slug_handles_spaces_punctuation_and_case():
    tasks = plan_tasks({"tasks": [_stt_task(name="Kitchen STT #1!")]})
    assert tasks[0].slug == "kitchen_stt_1"


def test_audio_format_mp3_accepted():
    tasks = plan_tasks({"tasks": [_tts_task(audio_format="mp3")]})
    assert tasks[0].audio_format == "mp3"


def test_invalid_audio_format_raises():
    with pytest.raises(ConfigError, match="'audio_format' must be one of"):
        plan_tasks({"tasks": [_tts_task(audio_format="ogg")]})
