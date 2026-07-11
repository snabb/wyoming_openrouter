"""Release metadata consistency tests."""

import re
import tomllib
from pathlib import Path

from wyoming_openrouter import __version__

_PROJECT_ROOT = Path(__file__).parents[1]


def test_release_versions_match():
    pyproject = tomllib.loads((_PROJECT_ROOT / "pyproject.toml").read_text())
    project_version = pyproject["project"]["version"]

    config_text = (_PROJECT_ROOT / "config.yaml").read_text()
    config_match = re.search(
        r"^version:\s*['\"]?([^'\"\s]+)", config_text, re.MULTILINE
    )
    assert config_match is not None
    app_version = config_match.group(1)

    assert __version__ == project_version == app_version
