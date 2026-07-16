"""Tests for agent/runtime_cwd.py — the single source of truth for the agent working directory."""

import os
from pathlib import Path

import pytest

import agent.runtime_cwd as rt
from agent.runtime_cwd import (
    clear_session_cwd,
    resolve_agent_cwd,
    resolve_context_cwd,
    set_session_cwd,
)


def _raise_oserror(*args, **kwargs):
    raise OSError("cwd gone")


class TestResolveAgentCwd:
    def test_prefers_terminal_cwd_over_getcwd(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        monkeypatch.chdir(os.path.expanduser("~"))
        assert resolve_agent_cwd() == tmp_path

    def test_falls_back_to_getcwd_when_unset(self, monkeypatch, tmp_path):
        # The #19242 local-CLI contract: TERMINAL_CWD is unset, so the launch dir wins.
        monkeypatch.delenv("TERMINAL_CWD", raising=False)
        monkeypatch.chdir(tmp_path)
        assert resolve_agent_cwd() == tmp_path

    def test_skips_nonexistent_terminal_cwd(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path / "gone"))
        monkeypatch.chdir(tmp_path)
        assert resolve_agent_cwd() == tmp_path

    def test_expands_leading_tilde(self, monkeypatch):
        monkeypatch.setenv("TERMINAL_CWD", "~")
        assert resolve_agent_cwd() == Path(os.path.expanduser("~"))

    def test_whitespace_only_terminal_cwd_falls_back_to_getcwd(self, monkeypatch, tmp_path):
        # "   ".strip() → "" → falsy, so the launch dir wins (not a "   " path).
        monkeypatch.setenv("TERMINAL_CWD", "   ")
        monkeypatch.chdir(tmp_path)
        assert resolve_agent_cwd() == tmp_path

    def test_propagates_oserror_from_getcwd(self, monkeypatch):
        # The fallback arm calls os.getcwd(), which can raise OSError (deleted cwd).
        # The resolver must NOT swallow it — build_environment_hints owns the
        # try/except OSError guard at the call site (prompt_builder.py:805).
        monkeypatch.delenv("TERMINAL_CWD", raising=False)
        monkeypatch.setattr(rt.os, "getcwd", _raise_oserror)
        with pytest.raises(OSError):
            resolve_agent_cwd()


class TestResolveContextCwd:
    def test_returns_dir_when_set(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        assert resolve_context_cwd() == tmp_path

    def test_returns_none_when_unset(self, monkeypatch):
        # Unset → None; the caller (build_context_files_prompt) then getcwds —
        # the local-CLI #19242 contract. Discovery still runs; it is NOT skipped.
        monkeypatch.delenv("TERMINAL_CWD", raising=False)
        assert resolve_context_cwd() is None

    def test_returns_nonexistent_dir_with_warning(self, monkeypatch, tmp_path, caplog):
        # Deliberate asymmetry vs resolve_agent_cwd: an explicitly configured
        # path is honored even when missing (discovery just finds nothing), but
        # it now WARNS so a typo'd terminal.cwd is visible in the logs instead
        # of silently resolving somewhere else (#64590).
        import logging

        missing = tmp_path / "gone"
        monkeypatch.setenv("TERMINAL_CWD", str(missing))
        with caplog.at_level(logging.WARNING, logger="agent.runtime_cwd"):
            assert resolve_context_cwd() == missing
        assert any("does not exist" in r.message for r in caplog.records)

    def test_returns_install_tree_when_explicitly_configured(self, monkeypatch):
        # An EXPLICIT TERMINAL_CWD pointing at the Hermes checkout is honored —
        # developers working ON hermes-agent want its AGENTS.md. Only the
        # unconfigured os.getcwd() fallback in build_context_files_prompt is
        # guarded against the install tree (#64590).
        monkeypatch.setenv("TERMINAL_CWD", str(rt._PACKAGE_ROOT))
        assert resolve_context_cwd() == rt._PACKAGE_ROOT

    def test_expands_leading_tilde(self, monkeypatch):
        monkeypatch.setenv("TERMINAL_CWD", "~")
        assert resolve_context_cwd() == Path(os.path.expanduser("~"))

    def test_whitespace_only_terminal_cwd_returns_none(self, monkeypatch):
        # "   ".strip() → "" → None, so the caller getcwds for discovery rather
        # than building Path("   ") and resolving garbage under the launch dir.
        monkeypatch.setenv("TERMINAL_CWD", "   ")
        assert resolve_context_cwd() is None


class TestSessionCwdOverride:
    """The #29531 per-session arm: a contextvar cwd wins over TERMINAL_CWD so a
    multi-session gateway can pin each session to its own folder."""

    def test_session_cwd_overrides_terminal_cwd(self, monkeypatch, tmp_path):
        other = tmp_path / "other"
        other.mkdir()
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        token = set_session_cwd(str(other))
        try:
            assert resolve_agent_cwd() == other
            assert resolve_context_cwd() == other
        finally:
            rt._SESSION_CWD.reset(token)

    def test_empty_session_cwd_falls_back_to_terminal_cwd(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        token = set_session_cwd("")
        try:
            assert resolve_agent_cwd() == tmp_path
            assert resolve_context_cwd() == tmp_path
        finally:
            rt._SESSION_CWD.reset(token)

    def test_clear_session_cwd_restores_terminal_cwd(self, monkeypatch, tmp_path):
        other = tmp_path / "other"
        other.mkdir()
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        token = set_session_cwd(str(other))
        try:
            clear_session_cwd()
            assert resolve_agent_cwd() == tmp_path
        finally:
            rt._SESSION_CWD.reset(token)

    def test_nonexistent_session_cwd_falls_back(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        token = set_session_cwd(str(tmp_path / "gone"))
        try:
            # resolve_agent_cwd guards on isdir; a missing session cwd must not win.
            assert resolve_agent_cwd() == tmp_path
        finally:
            rt._SESSION_CWD.reset(token)


class TestIsInstallTree:
    def test_package_root_itself(self):
        assert rt.is_install_tree(rt._PACKAGE_ROOT) is True

    def test_path_inside_package_root(self):
        assert rt.is_install_tree(rt._PACKAGE_ROOT / "agent") is True

    def test_ancestor_of_package_root_is_not_install_tree(self):
        # A user home that happens to contain the checkout is a legitimate
        # workspace and must not be blocked.
        assert rt.is_install_tree(rt._PACKAGE_ROOT.parent) is False

    def test_unrelated_path(self, tmp_path):
        assert rt.is_install_tree(tmp_path) is False


class TestResolveContextFilesCwd:
    """Surface-aware discovery cwd (#64590): explicit config always wins; only
    daemon surfaces suppress the launch-dir fallback."""

    def test_configured_cwd_wins_on_any_surface(self, monkeypatch, tmp_path):
        from agent.system_prompt import resolve_context_files_cwd

        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        assert resolve_context_files_cwd("tui") == str(tmp_path)
        assert resolve_context_files_cwd("cli") == str(tmp_path)

    def test_cli_falls_back_to_launch_dir(self, monkeypatch, tmp_path):
        from agent.system_prompt import resolve_context_files_cwd

        monkeypatch.delenv("TERMINAL_CWD", raising=False)
        monkeypatch.chdir(tmp_path)
        assert resolve_context_files_cwd("cli") == str(tmp_path)
        # Unset platform defaults to the CLI contract.
        assert resolve_context_files_cwd(None) == str(tmp_path)

    def test_daemon_surfaces_return_none_when_unconfigured(self, monkeypatch, tmp_path):
        from agent.system_prompt import resolve_context_files_cwd

        monkeypatch.delenv("TERMINAL_CWD", raising=False)
        monkeypatch.chdir(tmp_path)
        for platform in ("tui", "telegram", "discord", "cron", "desktop"):
            assert resolve_context_files_cwd(platform) is None

    def test_daemon_in_install_tree_end_to_end_skips_agents_md(self, monkeypatch, tmp_path):
        # E2E through the discovery path: a daemon surface with no configured
        # cwd, process cwd inside the (fake) install tree → no project context.
        from agent.prompt_builder import build_context_files_prompt
        from agent.system_prompt import resolve_context_files_cwd

        monkeypatch.setattr(rt, "_PACKAGE_ROOT", tmp_path.resolve())
        (tmp_path / "AGENTS.md").write_text("contributor guide")
        monkeypatch.delenv("TERMINAL_CWD", raising=False)
        monkeypatch.chdir(tmp_path)
        result = build_context_files_prompt(
            cwd=resolve_context_files_cwd("tui"), skip_soul=True
        )
        assert result == ""

    def test_cli_in_install_tree_end_to_end_loads_agents_md(self, monkeypatch, tmp_path):
        # The interactive CLI launched from the checkout keeps its AGENTS.md.
        from agent.prompt_builder import build_context_files_prompt
        from agent.system_prompt import resolve_context_files_cwd

        monkeypatch.setattr(rt, "_PACKAGE_ROOT", tmp_path.resolve())
        (tmp_path / "AGENTS.md").write_text("contributor guide")
        monkeypatch.delenv("TERMINAL_CWD", raising=False)
        monkeypatch.chdir(tmp_path)
        result = build_context_files_prompt(
            cwd=resolve_context_files_cwd("cli"), skip_soul=True
        )
        assert "contributor guide" in result

    def test_explicit_terminal_cwd_install_tree_end_to_end_loads_agents_md(
        self, monkeypatch, tmp_path
    ):
        # Explicit TERMINAL_CWD at the checkout: even a daemon surface loads it.
        from agent.prompt_builder import build_context_files_prompt
        from agent.system_prompt import resolve_context_files_cwd

        monkeypatch.setattr(rt, "_PACKAGE_ROOT", tmp_path.resolve())
        (tmp_path / "AGENTS.md").write_text("contributor guide")
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        result = build_context_files_prompt(
            cwd=resolve_context_files_cwd("tui"), skip_soul=True
        )
        assert "contributor guide" in result
