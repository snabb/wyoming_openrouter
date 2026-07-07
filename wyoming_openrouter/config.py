"""Task configuration: load, validate, and flatten the tasks config file.

The config file's shape is identical whether it's Supervisor's own
/data/options.json (Home Assistant App) or a user-supplied --config file
(standalone Docker/CLI) -- one flat "tasks" list, each task fully
self-contained (including its own OpenRouter API key), no cross-referencing.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .util import slugify

# IANA "registered" range: excludes both privileged ports (<1024, need root)
# and the dynamic/ephemeral range (49152-65535, used transiently by the OS
# for outgoing connections and prone to collide with a persistent listener).
MIN_VALID_PORT = 1024
MAX_VALID_PORT = 49151

VALID_TYPES = frozenset({"stt", "tts"})
VALID_AUDIO_FORMATS = frozenset({"pcm", "mp3"})


class ConfigError(Exception):
    """Raised for any invalid task configuration."""


@dataclass
class TaskConfig:
    """One configured STT or TTS task, fully self-contained."""

    name: str
    api_key: str
    type: str
    port: int
    model: str
    timeout: float = 60.0
    # STT-specific
    language: Optional[str] = None
    default_language: Optional[str] = None
    temperature: Optional[float] = None
    # TTS-specific
    voice: Optional[str] = None
    speed: float = 1.0
    # Which format to request from OpenRouter for a tts task: "pcm" needs no
    # local decode but not every model supports it (some, e.g. Mistral's
    # voxtral-mini-tts, only offer mp3); "mp3" is smaller over the wire (a
    # real consideration on a slow link to OpenRouter) but costs a local
    # mpg123 decode pass before Wyoming delivery (which always needs raw PCM
    # regardless of this setting).
    audio_format: str = "pcm"
    # Shared advanced passthrough (raw OpenRouter "provider" object)
    provider: Optional[dict[str, Any]] = None
    slug: str = field(init=False)

    def __post_init__(self) -> None:
        self.slug = slugify(self.name)


def _require(raw: dict[str, Any], key: str, index: int, name: str = "") -> Any:
    value = raw.get(key)
    if value in (None, ""):
        label = f"tasks[{index}] ({name})" if name else f"tasks[{index}]"
        raise ConfigError(f"{label}: '{key}' is required")
    return value


def _parse_task(raw: dict[str, Any], index: int) -> TaskConfig:
    name = str(_require(raw, "name", index))
    api_key = str(_require(raw, "api_key", index, name))
    task_type = str(_require(raw, "type", index, name))
    if task_type not in VALID_TYPES:
        raise ConfigError(
            f"tasks[{index}] ({name}): 'type' must be one of "
            f"{sorted(VALID_TYPES)}, got {task_type!r}"
        )
    port = int(_require(raw, "port", index, name))
    model = str(_require(raw, "model", index, name))

    provider: Optional[dict[str, Any]] = None
    provider_raw = raw.get("provider")
    if provider_raw:
        try:
            provider = json.loads(provider_raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"tasks[{index}] ({name}): 'provider' is not valid JSON: {exc}"
            ) from exc

    voice = raw.get("voice") or None
    if task_type == "tts" and not voice:
        raise ConfigError(
            f"tasks[{index}] ({name}): 'voice' is required for a tts task"
        )

    language = raw.get("language") or None
    if task_type == "stt" and not language:
        # Advertised to Home Assistant as this task's sole supported_language
        # (see stt_handler.get_stt_wyoming_info) -- an empty value there
        # silently produces an entity that supports zero languages and can
        # never be selected in an Assist pipeline, with no error anywhere to
        # explain why. Fail loudly here instead.
        raise ConfigError(
            f"tasks[{index}] ({name}): 'language' is required for a stt task"
        )

    audio_format = raw.get("audio_format") or "pcm"
    if audio_format not in VALID_AUDIO_FORMATS:
        raise ConfigError(
            f"tasks[{index}] ({name}): 'audio_format' must be one of "
            f"{sorted(VALID_AUDIO_FORMATS)}, got {audio_format!r}"
        )

    temperature_raw = raw.get("temperature")
    temperature = float(temperature_raw) if temperature_raw not in (None, "") else None

    return TaskConfig(
        name=name,
        api_key=api_key,
        type=task_type,
        port=port,
        model=model,
        timeout=float(raw.get("timeout") or 60.0),
        language=language,
        default_language=raw.get("default_language") or None,
        temperature=temperature,
        voice=voice,
        speed=float(raw.get("speed") or 1.0),
        audio_format=audio_format,
        provider=provider,
    )


def plan_tasks(data: dict[str, Any]) -> list[TaskConfig]:
    """Parse and validate the raw config dict into a flat, ready-to-serve task list."""
    raw_tasks = data.get("tasks") or []
    if not raw_tasks:
        raise ConfigError("no tasks configured; at least one is required")

    tasks = [_parse_task(raw, i) for i, raw in enumerate(raw_tasks)]

    seen_ports: dict[int, str] = {}
    for task in tasks:
        if not (MIN_VALID_PORT <= task.port <= MAX_VALID_PORT):
            raise ConfigError(
                f"task '{task.name}': port {task.port} must be between "
                f"{MIN_VALID_PORT} and {MAX_VALID_PORT}"
            )
        if task.port in seen_ports:
            raise ConfigError(
                f"tasks '{seen_ports[task.port]}' and '{task.name}' both use "
                f"port {task.port}"
            )
        seen_ports[task.port] = task.name

    return tasks


def load_config(path: str) -> list[TaskConfig]:
    """Load and validate the tasks config file at ``path``."""
    raw_text = Path(path).read_text()
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path}: invalid JSON: {exc}") from exc
    return plan_tasks(data)
