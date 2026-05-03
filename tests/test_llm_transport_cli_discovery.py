"""Tests for ClaudeCliTransport's CLI binary auto-discovery.

The launchd daemon plist runs with PATH=/usr/bin:/bin:/usr/sbin:/sbin
unless explicitly extended. The Claude Code CLI installs into
/opt/homebrew/bin/claude (or ~/.npm-global/bin/claude). Without
auto-discovery, every debate tick fails with CliNotAvailable.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path
from unittest import mock

from trading_bot.shared.llm_transport import (
    ClaudeCliTransport,
    _resolve_cli_path,
)


def _make_executable(path: Path) -> None:
    path.write_text("#!/bin/sh\necho ok\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def test_resolve_absolute_path_passes_through(tmp_path: Path) -> None:
    binary = tmp_path / "claude"
    _make_executable(binary)
    assert _resolve_cli_path(str(binary)) == str(binary)


def test_resolve_finds_on_path(tmp_path: Path, monkeypatch) -> None:
    binary = tmp_path / "claude"
    _make_executable(binary)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert _resolve_cli_path("claude") == str(binary)


def test_resolve_falls_back_to_homebrew_when_path_minimal(
    tmp_path: Path, monkeypatch,
) -> None:
    fake_brew = tmp_path / "claude"
    _make_executable(fake_brew)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # no /opt/homebrew
    with mock.patch(
        "trading_bot.shared.llm_transport._CLI_FALLBACK_LOCATIONS",
        (str(fake_brew),),
    ):
        assert _resolve_cli_path("claude") == str(fake_brew)


def test_resolve_returns_original_when_nothing_found(monkeypatch) -> None:
    monkeypatch.setenv("PATH", "/nonexistent")
    with mock.patch(
        "trading_bot.shared.llm_transport._CLI_FALLBACK_LOCATIONS",
        ("/nope/claude", "/also/missing/claude"),
    ):
        assert _resolve_cli_path("claude") == "claude"


def test_transport_init_resolves_cli_path(
    tmp_path: Path, monkeypatch,
) -> None:
    fake_brew = tmp_path / "claude"
    _make_executable(fake_brew)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # launchd-style minimal
    with mock.patch(
        "trading_bot.shared.llm_transport._CLI_FALLBACK_LOCATIONS",
        (str(fake_brew),),
    ):
        t = ClaudeCliTransport(role_name="scout_skeptic")
    assert t.cli_path == str(fake_brew), (
        "transport must resolve to absolute path so subprocess.run "
        "doesn't depend on launchd PATH"
    )


def test_transport_respects_explicit_absolute_cli_path(tmp_path: Path) -> None:
    binary = tmp_path / "myclaude"
    _make_executable(binary)
    t = ClaudeCliTransport(role_name="x", cli_path=str(binary))
    assert t.cli_path == str(binary)
