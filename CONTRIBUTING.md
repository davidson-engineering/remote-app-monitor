# Contributing

## Development

```bash
uv sync --all-extras
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

The serial tests use pseudo-terminals, so they run on macOS and Linux and are
skipped on Windows. CI runs everything on Linux, macOS and Windows for each
supported Python version.

## Releasing

Releases are published to PyPI by `.github/workflows/release.yml` when a
GitHub release is published.

1. Set `__version__` in `src/app_monitor/__init__.py` and merge to `main`.
2. Create a GitHub release with the tag `v<version>` (e.g. `v0.2.0`). The
   workflow checks the tag matches `__version__`, builds, and uploads.

### One-time PyPI setup

The workflow uses [trusted publishing](https://docs.pypi.org/trusted-publishers/),
so no API token is stored in GitHub. Before the first release:

1. On pypi.org, go to *Your projects > Publishing* and add a **pending
   publisher** (the project doesn't exist on PyPI until its first upload):
   - PyPI project name: `remote-app-monitor`
   - Owner: `davidson-engineering`
   - Repository: `remote-app-monitor`
   - Workflow: `release.yml`
   - Environment: `pypi`
2. In the GitHub repository settings, create an environment named `pypi`.
   Adding yourself as a required reviewer makes each upload wait for your
   approval.
