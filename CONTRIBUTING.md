# Contributing

## Development Setup

1. Install dependencies: `uv sync --all-extras --dev`
2. Make your changes.
3. Ensure pre-commit hooks pass: `prek run --all-files`
4. Ensure tests pass: `uv run pytest`
5. Submit a PR.

## Releasing a New Version

The version must be updated in **three** places (keep them in sync):

1. `pyproject.toml` — `version = "X.Y.Z"`
2. `wyoming_openrouter/__init__.py` — `__version__ = "X.Y.Z"`
3. `config.yaml` — `version: X.Y.Z` (Home Assistant app version)

`tests/test_versions.py` enforces that these values remain equal before an
image can be published.

Then update `CHANGELOG.md` with the changes.

Commit and push to `master`. CI then builds and publishes
`ghcr.io/snabb/wyoming_openrouter:<version>` (alongside `latest`)
automatically — no manual tagging step needed for the Docker image itself.

The git tag and GitHub Release are **not** automated and must be created by
hand once CI is green:

```bash
git tag -a vX.Y.Z -m "Release X.Y.Z"
git push origin vX.Y.Z
gh release create vX.Y.Z --title "vX.Y.Z" --notes "$(sed -n '/^## \[X.Y.Z\]/,/^## \[/p' CHANGELOG.md | sed '1d;$d')"
```

Without this, the version bump and image publish still work, but the repo's
"Latest release" on GitHub stays stuck on the previous version.
