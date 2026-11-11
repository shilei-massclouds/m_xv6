"""Keep runtime boot snapshots inside each test's temporary directory."""

import pytest

from framework import snapshot_paths


@pytest.fixture(autouse=True)
def isolate_snapshot_paths(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path_factory.mktemp("boot") / "snapshots"
    directory.mkdir()
    monkeypatch.setattr(snapshot_paths, "SNAPSHOT_DIR", directory)
    monkeypatch.setattr(
        snapshot_paths,
        "BP_INITIALIZED_SNAPSHOT_PATH",
        directory / "bp_initialized.snap",
    )
