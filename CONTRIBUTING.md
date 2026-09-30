# Contributing

## Development

```bash
uv sync --all-extras
uv run playwright install chromium    # once, for the browser tests
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

The serial tests use pseudo-terminals, so they run on macOS and Linux and are
skipped on Windows. CI runs everything on Linux, macOS and Windows for each
supported Python version, except the browser tests (`-m browser`), which run
once, in Chromium on Linux: the page's JavaScript is the same everywhere.

`uv run --all-extras python docs/screenshot.py` retakes the README's
screenshot after a change to the launch control example.

## Releasing

Releases are published to PyPI by `.github/workflows/release.yml` when a
GitHub release is published.

1. Set `__version__` in `src/sightglass/__init__.py` and merge to `main`.
2. Create a GitHub release with the tag `v<version>` (e.g. `v0.2.0`). The
   workflow checks the tag matches `__version__`, builds, and uploads.

### One-time PyPI setup

The workflow uses [trusted publishing](https://docs.pypi.org/trusted-publishers/),
so no API token is stored in GitHub. Before the first release:

1. On pypi.org, go to *Your projects > Publishing* and add a **pending
   publisher** (the project doesn't exist on PyPI until its first upload):
   - PyPI project name: `sightglass`
   - Owner: `davidson-engineering`
   - Repository: `sightglass`
   - Workflow: `release.yml`
   - Environment: `pypi`
2. In the GitHub repository settings, create an environment named `pypi`.
   Adding yourself as a required reviewer makes each upload wait for your
   approval.
