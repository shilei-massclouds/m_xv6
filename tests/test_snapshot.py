"""Generic snapshots keep optional I point provenance backward compatible."""

import json
from dataclasses import dataclass

import pytest

from framework.snapshot import SnapshotFormatError, load_snapshot, save_snapshot


@dataclass
class Root:
    value: int = 3
    self_ref: object | None = None


@pytest.mark.parametrize("point", [None, "example_0.2"])
def test_snapshot_roundtrip_with_optional_input_point(tmp_path, point):
    path = tmp_path / "output.snap"
    original = Root()
    original.self_ref = original
    save_snapshot(
        original, path, checkpoint="output", model_version=9, input_point=point
    )
    restored = Root(value=0)
    metadata = load_snapshot(
        restored, path, expected_checkpoint="output", model_version=9
    )
    assert restored.value == 3
    assert restored.self_ref is restored
    assert metadata.get("input_point") == point
    assert ("input_point" in metadata) is (point is not None)


@pytest.mark.parametrize("point", ["", 3])
def test_invalid_input_point_is_rejected_before_writing(tmp_path, point):
    path = tmp_path / "output.snap"
    with pytest.raises(ValueError, match="input_point"):
        save_snapshot(
            Root(), path, checkpoint="output", model_version=9, input_point=point
        )
    assert not path.exists()


def test_invalid_optional_input_point_in_header_is_rejected(tmp_path):
    path = tmp_path / "output.snap"
    save_snapshot(Root(), path, checkpoint="output", model_version=9)
    magic, header, rest = path.read_bytes().split(b"\n", 2)
    metadata = json.loads(header)
    metadata["input_point"] = 3
    path.write_bytes(magic + b"\n" + json.dumps(metadata).encode() + b"\n" + rest)
    with pytest.raises(SnapshotFormatError, match="input_point"):
        load_snapshot(Root(), path, model_version=9)
