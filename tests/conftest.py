"""Keep runtime boot snapshots inside each test's temporary directory."""

from pathlib import Path

import pytest

import snapshot_paths


@pytest.fixture(autouse=True)
def isolate_snapshot_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = tmp_path / "snapshots"
    monkeypatch.setattr(snapshot_paths, "SNAPSHOT_DIR", directory)
    monkeypatch.setattr(
        snapshot_paths,
        "BP_INITIALIZED_SNAPSHOT_PATH",
        directory / "bp_initialized.snap",
    )
