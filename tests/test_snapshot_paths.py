"""Automatic snapshot naming stays inside the configured directory."""

from pathlib import Path

import pytest

from framework import snapshot_paths


def test_snapshot_path_uses_configured_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setattr(snapshot_paths, "SNAPSHOT_DIR", tmp_path)

    assert snapshot_paths.snapshot_path("example", position=12, branch=3) == (
        tmp_path / "example_3.12.snap"
    )


@pytest.mark.parametrize("flow", ["", "../example", "nested/example", "a.b"])
def test_invalid_flow_name_is_rejected(flow: str):
    with pytest.raises(ValueError, match="flow name"):
        snapshot_paths.snapshot_path(flow, 0)


@pytest.mark.parametrize("position,branch", [(-1, 0), (0, -1), (True, 0), (0, True)])
def test_invalid_snapshot_number_is_rejected(position: int, branch: int):
    with pytest.raises(ValueError, match="nonnegative integer"):
        snapshot_paths.snapshot_path("example", position, branch)
