"""Behavioral regression coverage for the wheel/sdist distribution guard."""

import os
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _build_sdist(tmp_path, *, nix_build: bool) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    # nix develop exports this too, so it must not grant permission to build
    # a distributable artifact.
    env["NIX_BUILD_TOP"] = "/build/devshell"
    if nix_build:
        env["HERMES_NIX_BUILD"] = "1"
    else:
        env.pop("HERMES_NIX_BUILD", None)
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from setuptools.build_meta import build_sdist; build_sdist(r'{}')".format(tmp_path),
        ],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_sdist_rejects_nix_development_shell_environment(tmp_path):
    result = _build_sdist(tmp_path, nix_build=False)

    assert result.returncode != 0
    assert "Building wheels or sdists for hermes-agent is not supported" in result.stderr


def test_sdist_allows_explicit_nix_package_build_marker(tmp_path):
    result = _build_sdist(tmp_path, nix_build=True)

    assert result.returncode == 0, result.stderr
    assert list(tmp_path.glob("hermes_agent-*.tar.gz"))