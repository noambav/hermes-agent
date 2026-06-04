"""Regression coverage for the #38889 node migration-heal + stale-prune.

The user-facing fix reviewers cared about most — re-running the installer (or
``hermes update``) on a box whose node symlinks landed in the wrong (off-PATH)
dir re-links node into the canonical command dir AND prunes the stale shadow
copies — lives entirely in shell (``link_bundled_node`` in ``install.sh`` and
``_nb_link_bundled_node`` in ``scripts/lib/node-bootstrap.sh``).  Before this
file it had no automated coverage at all (only a manual VM run).

These tests drive the sourceable ``node-bootstrap.sh`` helper directly.  The FHS
``/usr/local/bin`` target requires root, so to exercise the same relink+prune
code path with a *writable* link dir we run in Termux mode (``$PREFIX/bin`` is
the link dir), which makes ``~/.local/bin`` one of the scanned stale dirs.  The
prune logic, safety guards, idempotency, and the ``set -e`` hardening are
identical across the FHS and Termux link dirs.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import NamedTuple

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
NODE_BOOTSTRAP = REPO_ROOT / "scripts" / "lib" / "node-bootstrap.sh"
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"

pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win") or shutil.which("bash") is None,
    reason="POSIX shell required to drive node-bootstrap.sh",
)


class _Layout(NamedTuple):
    home: Path
    hermes_home: Path
    node_bin: Path
    prefix: Path
    link_dir: Path
    local_bin: Path


def _layout(tmp_path: Path) -> _Layout:
    """The fixed dir layout these tests share.

    Termux mode (PREFIX contains ``com.termux/files/usr``) makes the link dir
    ``$PREFIX/bin``, so ``~/.local/bin`` is a *scanned, writable* stale dir —
    the only way to exercise the relink+prune without being root.
    """
    home = tmp_path / "home"
    hermes_home = tmp_path / "hermes"
    prefix = tmp_path / "termux" / "com.termux" / "files" / "usr"
    return _Layout(
        home=home,
        hermes_home=hermes_home,
        node_bin=hermes_home / "node" / "bin",
        prefix=prefix,
        link_dir=prefix / "bin",
        local_bin=home / ".local" / "bin",
    )


def _make_bundled_node(hermes_home: Path) -> Path:
    """Create dummy <HERMES_HOME>/node/bin/{node,npm,npx} executables."""
    node_bin = hermes_home / "node" / "bin"
    node_bin.mkdir(parents=True)
    for name in ("node", "npm", "npx"):
        exe = node_bin / name
        exe.write_text("#!/bin/sh\necho dummy\n")
        exe.chmod(0o755)
    return node_bin


def _run_nb_link(tmp_path: Path, *, extra: str = "") -> subprocess.CompletedProcess:
    """Source node-bootstrap.sh in Termux mode and run _nb_link_bundled_node.

    Runs under ``set -e`` so the prune's best-effort ``rm`` failures must not
    abort (the #38889 hardening); ``SENTINEL_OK`` after the call proves we
    returned normally.
    """
    lay = _layout(tmp_path)
    lay.link_dir.mkdir(parents=True, exist_ok=True)
    lay.local_bin.mkdir(parents=True, exist_ok=True)

    env = {
        "HOME": str(lay.home),
        "PREFIX": str(lay.prefix),
        "HERMES_HOME": str(lay.hermes_home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    script = textwrap.dedent(
        f"""
        set -e
        source "{NODE_BOOTSTRAP}"
        {extra}
        _nb_link_bundled_node
        echo SENTINEL_OK
        """
    )
    return subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True
    )


def test_relinks_and_prunes_stale_hermes_shadows(tmp_path: Path) -> None:
    """node/npm links into the bundle's node dir are pruned; the canonical link
    dir gets fresh links; non-hermes links and real files are left alone."""
    lay = _layout(tmp_path)
    node_bin = _make_bundled_node(lay.hermes_home)
    link_dir = lay.link_dir
    local_bin = lay.local_bin

    # Simulate an old/broken install: hermes-owned shadow links in ~/.local/bin.
    local_bin.mkdir(parents=True, exist_ok=True)
    (local_bin / "node").symlink_to(node_bin / "node")           # hermes → PRUNE
    (local_bin / "npm").write_text("#!/bin/sh\n")                 # real file → KEEP
    (local_bin / "npm").chmod(0o755)
    nvm_npx = tmp_path / "fake_nvm" / "bin" / "npx"
    nvm_npx.parent.mkdir(parents=True)
    nvm_npx.write_text("#!/bin/sh\n")
    (local_bin / "npx").symlink_to(nvm_npx)                      # user link → KEEP

    result = _run_nb_link(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "SENTINEL_OK" in result.stdout

    # Canonical link dir now has all three pointing into the bundle.
    for name in ("node", "npm", "npx"):
        link = link_dir / name
        assert link.is_symlink(), f"{name} not linked into canonical dir"
        assert link.resolve() == (node_bin / name).resolve()

    # Stale hermes shadow was pruned; the real file and the nvm link survived.
    assert not (local_bin / "node").exists() and not (local_bin / "node").is_symlink(), (
        "stale hermes-owned ~/.local/bin/node should have been pruned"
    )
    assert (local_bin / "npm").is_file() and not (local_bin / "npm").is_symlink(), (
        "a real binary must never be removed by the prune"
    )
    assert (local_bin / "npx").is_symlink() and (local_bin / "npx").resolve() == nvm_npx.resolve(), (
        "a user's nvm/fnm link must never be removed by the prune"
    )


def test_idempotent_across_repeated_runs(tmp_path: Path) -> None:
    """Running the heal twice converges to the same state (no thrash/dup)."""
    lay = _layout(tmp_path)
    node_bin = _make_bundled_node(lay.hermes_home)
    link_dir = lay.link_dir

    first = _run_nb_link(tmp_path)
    assert first.returncode == 0, first.stderr
    # Second run with the canonical links already in place.
    second = _run_nb_link(tmp_path)
    assert second.returncode == 0, second.stderr
    assert "SENTINEL_OK" in second.stdout
    for name in ("node", "npm", "npx"):
        link = link_dir / name
        assert link.is_symlink()
        assert link.resolve() == (node_bin / name).resolve()


def test_prune_failure_does_not_abort_under_set_e(tmp_path: Path) -> None:
    """A non-removable stale shadow (read-only parent dir) must NOT abort the
    caller under ``set -e`` — the #38889 prune-abort hardening."""
    lay = _layout(tmp_path)
    node_bin = _make_bundled_node(lay.hermes_home)
    local_bin = lay.local_bin
    local_bin.mkdir(parents=True)
    (local_bin / "node").symlink_to(node_bin / "node")  # hermes shadow to prune

    # Make the stale dir read-only so unlinking the shadow fails with EACCES
    # (non-root cannot unlink in a dir without write perm).
    local_bin.chmod(0o555)
    try:
        result = _run_nb_link(tmp_path)
    finally:
        local_bin.chmod(0o755)  # restore so tmp cleanup can proceed

    assert result.returncode == 0, (
        "set -e abort regression (#38889): a failing best-effort prune must not "
        f"fail the caller.\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "SENTINEL_OK" in result.stdout


def test_install_sh_prune_is_set_e_safe_static() -> None:
    """Static guard for the same fix in install.sh's link_bundled_node (which
    can't be sourced standalone): the stale-prune rm must be guarded and the
    function must end with `return 0` so it never trips `set -e`."""
    text = INSTALL_SH.read_text()
    match = re.search(
        r"link_bundled_node\(\)\s*\{.*?\n\}",
        text,
        re.DOTALL,
    )
    assert match is not None, "could not locate link_bundled_node() in install.sh"
    body = match.group(0)
    assert 'rm -f "$stale_dir/$name" 2>/dev/null || true' in body, (
        "link_bundled_node prune rm must be `2>/dev/null || true` so a failed "
        "unlink under `set -e` doesn't abort the installer (#38889)"
    )
    assert re.search(r"return 0\s*\n\}", body), (
        "link_bundled_node must end with `return 0` so a failing prune is never "
        "the function's exit status under `set -e` (#38889)"
    )
