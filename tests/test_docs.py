"""The README and the agent guide are contracts: keep them true to the code."""

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest

import app_monitor
import app_monitor.client
from app_monitor.cli import guide, parser

ROOT = Path(__file__).parent.parent
DOCS = {
    "README.md": ROOT / "README.md",
    "GUIDE.md": ROOT / "src" / "app_monitor" / "GUIDE.md",
    "examples/README.md": ROOT / "examples" / "README.md",
}
EXAMPLES = sorted((ROOT / "examples").rglob("*.py"))


def python_blocks(path: Path) -> list[str]:
    return re.findall(r"```python\n(.*?)```", path.read_text("utf-8"), re.S)


def check_imports(name: str, source: str) -> None:
    tree = ast.parse(source)  # raises on a syntax error
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and (node.module.startswith("app_monitor"))
        ):
            module = (
                app_monitor.client if node.module.endswith("client") else app_monitor
            )
            for alias in node.names:
                assert hasattr(module, alias.name), f"{name}: {alias.name}"


@pytest.mark.parametrize("name", [n for n in DOCS if n != "examples/README.md"])
def test_python_snippets_compile_and_import_real_names(name):
    blocks = python_blocks(DOCS[name])
    assert blocks
    for block in blocks:
        tree = ast.parse(block)  # raises on a syntax error
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and (node.module.startswith("app_monitor"))
            ):
                module = (
                    app_monitor.client
                    if node.module.endswith("client")
                    else app_monitor
                )
                for alias in node.names:
                    assert hasattr(module, alias.name), f"{name}: {alias.name}"


@pytest.mark.parametrize("name", DOCS)
def test_cli_flags_in_docs_exist(name):
    known = {option for action in parser()._actions for option in action.option_strings}
    text = DOCS[name].read_text("utf-8")
    # The command, not the package name inside "remote-app-monitor[...]".
    mentioned = set(re.findall(r"(?<![\w-])app-monitor\b[^\n`\"|]*", text))
    flags = {
        flag for line in mentioned for flag in re.findall(r"(--[a-z][a-z-]+)", line)
    }
    assert flags <= known, flags - known


def test_guide_command_prints_the_shipped_guide():
    result = subprocess.run(
        [sys.executable, "-m", "app_monitor", "--guide"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == guide() == DOCS["GUIDE.md"].read_text("utf-8")


def test_claude_md_imports_the_guide():
    assert "@src/app_monitor/GUIDE.md" in (ROOT / "CLAUDE.md").read_text("utf-8")


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_examples_compile_and_import_real_names(path):
    check_imports(path.name, path.read_text("utf-8"))


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_self_contained_examples_declare_their_dependencies(path):
    """Single-file examples must carry PEP 723 metadata, so `uv run <url>`
    works; examples in a folder of their own, and fake_device.py, need files
    from the repository and run from a clone."""
    needs_repo = path.parent != ROOT / "examples" or path.name == "fake_device.py"
    has_metadata = path.read_text("utf-8").startswith("# /// script")
    assert has_metadata != needs_repo


def test_example_run_commands_point_at_files_that_exist():
    text = DOCS["examples/README.md"].read_text("utf-8")
    base = "https://raw.githubusercontent.com/davidson-engineering/remote-app-monitor/main/"
    linked = re.findall(re.escape(base) + r"([\w/.]+)", text)
    assert linked
    for relative in linked:
        assert (ROOT / relative).exists(), relative
