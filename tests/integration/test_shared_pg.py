"""Pytest entry point for the shared-PostgreSQL integration harness.

The real work lives in ``shared_pg/run.sh`` (bash orchestrates Docker + two
host "user" environments). This wrapper just lets ``pytest -m integration``
discover it and skips cleanly when Docker or the CLI isn't available, so a
normal ``pytest`` run on a laptop without Docker stays green.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

RUN_SH = Path(__file__).parent / "shared_pg" / "run.sh"


def _docker_ready() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(
            ["docker", "info"], capture_output=True, timeout=15
        ).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


@pytest.mark.integration
def test_shared_pg_end_to_end():
    if not shutil.which("knowledge"):
        pytest.skip("knowledge CLI not on PATH")
    if not _docker_ready():
        pytest.skip("Docker not available")
    # Inherit the environment; run.sh manages its own isolated containers/homes.
    result = subprocess.run(["bash", str(RUN_SH)], timeout=900)
    assert result.returncode == 0, "shared-PG integration scenario failed (see output above)"
