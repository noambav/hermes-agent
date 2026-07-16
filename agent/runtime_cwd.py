"""Single source of truth for the agent working directory.

`TERMINAL_CWD` is the runtime carrier for the configured working directory
(design #19214/#19242: `terminal.cwd` is bridged once to `TERMINAL_CWD` at
gateway/cron startup). The local-CLI backend deliberately leaves it unset and
relies on the launch dir. Reading it in one place keeps the system prompt, the
tool surfaces, and context-file discovery agreeing on where the agent lives.

Multi-session gateways can pin a logical cwd via the `_SESSION_CWD`
contextvar; CLI/cron fall through to `TERMINAL_CWD`/launch cwd.
"""

import logging
import os
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_UNSET: Any = object()

_SESSION_CWD: ContextVar = ContextVar("HERMES_SESSION_CWD", default=_UNSET)

# The Python package/source root (this file lives at <root>/agent/runtime_cwd.py).
# Backends whose process cwd is an accident of spawning (the desktop's headless
# `hermes serve`) use this to avoid DEFAULTING sessions into the Hermes source
# tree, whose contributor AGENTS.md would otherwise load as project context
# (#64590). An explicitly chosen path — session cwd, TERMINAL_CWD, terminal.cwd,
# the launch dir of an interactive surface — is always honored, install tree
# included: a developer working ON hermes-agent wants that AGENTS.md.
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def is_install_tree(p: Path) -> bool:
    """True when *p* IS the Hermes package/source root or sits inside it.

    Ancestors of the package root (a user home that happens to contain the
    checkout, a --user site-packages parent) are legitimate workspaces and
    return False. Used only to steer *fallback* defaults away from the source
    tree — never to reject a path the user or config explicitly picked.
    """
    try:
        p = Path(p).resolve()
    except Exception:
        return False
    return p == _PACKAGE_ROOT or _PACKAGE_ROOT in p.parents


def set_session_cwd(cwd: str | None) -> Token:
    """Pin the logical cwd for the current context."""
    return _SESSION_CWD.set((cwd or "").strip())


def clear_session_cwd() -> None:
    _SESSION_CWD.set("")


def _session_cwd_override() -> str:
    value = _SESSION_CWD.get()
    if value is _UNSET:
        return ""
    return str(value).strip()


def resolve_agent_cwd() -> Path:
    override = _session_cwd_override()
    if override:
        p = Path(override).expanduser()
        if p.is_dir():
            return p
        logger.warning("configured working directory does not exist: %s", override)
    raw = os.environ.get("TERMINAL_CWD", "").strip()
    if raw:
        p = Path(raw).expanduser()
        if p.is_dir():
            return p
        logger.warning("TERMINAL_CWD does not exist: %s", raw)
    return Path(os.getcwd())


def resolve_context_cwd() -> Path | None:
    # None means "no configured cwd": build_context_files_prompt then falls back
    # to the launch dir (os.getcwd()) — correct for the local CLI, where the
    # launch dir IS the user's choice. Backend surfaces whose process cwd is
    # accidental avoid slurping the install dir at the *default* layer instead:
    # the gateway sets TERMINAL_CWD (see system_prompt.py), the TUI/desktop
    # gateway resolves each session's cwd up front (see tui_gateway/server.py
    # _fallback_spawn_cwd), and cron sets TERMINAL_CWD per workdir job.
    #
    # Explicitly configured paths are honored AS-IS — including a Hermes source
    # checkout (a developer working on hermes-agent wants its AGENTS.md loaded).
    # A configured-but-missing dir is returned too (discovery simply finds
    # nothing there); it only warns, so a typo'd terminal.cwd is visible in the
    # logs instead of silently steering discovery somewhere else (#64590).
    override = _session_cwd_override()
    if override:
        p = Path(override).expanduser()
        if not p.is_dir():
            logger.warning("configured working directory does not exist: %s", override)
        return p
    raw = os.environ.get("TERMINAL_CWD", "").strip()
    if raw:
        p = Path(raw).expanduser()
        if not p.is_dir():
            logger.warning("TERMINAL_CWD does not exist: %s", raw)
        return p
    return None
