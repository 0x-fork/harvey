"""Windows: text files are UTF-8 whatever the locale, and Claude is never run
through npm's claude.cmd shim.

Based on community PR #18 by Tariz Shahid.
"""

import ast
import io
from pathlib import Path

import pytest

from harvey import claude_runtime
from harvey.claude_runtime import find_claude as real_find_claude  # conftest stubs it

HARVEY = Path(__file__).resolve().parent.parent / "harvey"


def _text_io_without_encoding(tree):
    """open()/read_text()/write_text() calls in text mode with no encoding=."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name not in ("open", "read_text", "write_text"):
            continue
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and \
                func.value.id in ("os", "webbrowser", "aiosqlite", "sqlite3"):
            continue
        if any(k.arg == "encoding" for k in node.keywords):
            continue
        mode = next((a.value for a in node.args[1:2] if isinstance(a, ast.Constant)), None)
        mode = next((k.value.value for k in node.keywords
                     if k.arg == "mode" and isinstance(k.value, ast.Constant)), mode)
        if name == "open" and isinstance(mode, str) and "b" in mode:
            continue
        yield node.lineno


def test_every_text_file_is_read_and_written_as_utf8():
    # Windows defaults to the locale code page (cp1252): harvey train
    # crashed on "★" and prompts came out garbled.
    offenders = [
        f"{path.relative_to(HARVEY.parent)}:{line}"
        for path in sorted(HARVEY.rglob("*.py"))
        for line in _text_io_without_encoding(ast.parse(path.read_text(encoding="utf-8")))
    ]
    assert offenders == []


def test_config_and_prompts_with_non_ascii_text_load(tmp_path, monkeypatch):
    from harvey.brain import Brain
    import harvey.brain as brain_mod
    from harvey.config import load_config

    import yaml
    data = yaml.safe_load((HARVEY.parent / "harvey.yaml").read_text(encoding="utf-8"))
    data["persona"]["company"] = "Acme — Studio ★"
    config = tmp_path / "harvey.yaml"
    config.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    loaded = load_config(str(config))
    assert loaded.persona.company == "Acme — Studio ★"

    (tmp_path / "x.md").write_text("Rules ★ — {{name}}", encoding="utf-8")
    monkeypatch.setattr(brain_mod, "PROMPTS_DIR", tmp_path)
    assert Brain.load_prompt(object.__new__(Brain), "x", name="Zoë") == "Rules ★ — Zoë"


# ── Finding the Claude CLI on Windows ──

def _npm_install(tmp_path, with_native=True):
    shim = tmp_path / "npm" / "claude.CMD"
    shim.parent.mkdir()
    shim.write_text("@echo off\n", encoding="utf-8")
    native = shim.parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    if with_native:
        native.parent.mkdir(parents=True)
        native.write_bytes(b"MZ")
    return shim, native


def test_windows_npm_shim_resolves_to_the_native_executable(tmp_path, monkeypatch):
    shim, native = _npm_install(tmp_path)
    monkeypatch.setattr(claude_runtime, "_PLATFORM", "win32")
    monkeypatch.setattr(claude_runtime.shutil, "which", lambda *a, **k: str(shim))
    assert real_find_claude() == str(native)


def test_windows_npm_shim_without_the_executable_is_refused(tmp_path, monkeypatch):
    # Never through cmd.exe: it re-parses arguments, and the isolation flags
    # include empty ones (--tools "") whose meaning would change if dropped.
    shim, _ = _npm_install(tmp_path, with_native=False)
    monkeypatch.setattr(claude_runtime, "_PLATFORM", "win32")
    monkeypatch.setattr(claude_runtime.shutil, "which", lambda *a, **k: str(shim))
    with pytest.raises(claude_runtime.ClaudeRuntimeError) as err:
        real_find_claude()
    assert "claude.cmd" in str(err.value).lower() and "install" in str(err.value).lower()


@pytest.mark.parametrize("platform, found", [
    ("win32", r"C:\Users\me\.local\bin\claude.exe"),
    ("darwin", "/usr/local/bin/claude"),
    ("linux", "/home/me/.local/bin/claude"),
])
def test_other_installs_are_used_as_found(platform, found, monkeypatch):
    monkeypatch.setattr(claude_runtime, "_PLATFORM", platform)
    monkeypatch.setattr(claude_runtime.shutil, "which", lambda *a, **k: found)
    assert real_find_claude() == found


def test_not_installed_is_still_none(monkeypatch):
    monkeypatch.setattr(claude_runtime, "_PLATFORM", "win32")
    monkeypatch.setattr(claude_runtime.shutil, "which", lambda *a, **k: None)
    assert real_find_claude() is None


# ── Console output ──

def test_cli_output_never_crashes_on_a_legacy_code_page():
    # The dashboard runs `harvey run` with output going to a log file, which
    # Windows opens in cp1252: "⚠" or "→" in a message would crash print().
    from harvey.cli import _utf8_stdio
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252")
    _utf8_stdio(stream)
    print("⚠ sending paused → resume", file=stream)
    stream.flush()
    assert raw.getvalue().decode("utf-8").startswith("⚠ sending paused → resume")


def test_installing_the_missing_executable_changes_the_fingerprint(tmp_path, monkeypatch):
    # A paused brain re-checks the CLI only when this changes.
    shim, native = _npm_install(tmp_path, with_native=False)
    monkeypatch.setattr(claude_runtime, "_PLATFORM", "win32")
    monkeypatch.setattr(claude_runtime.shutil, "which", lambda *a, **k: str(shim))
    before = claude_runtime.fingerprint()
    native.parent.mkdir(parents=True)
    native.write_bytes(b"MZ")
    assert claude_runtime.fingerprint() != before
