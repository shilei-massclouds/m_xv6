"""Generic snapshots keep optional I point provenance backward compatible."""

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from framework import snapshot, snapshot_paths
from framework.snapshot import (
    SnapshotCompatibilityError,
    SnapshotError,
    SnapshotFormatError,
    deduplicate_outputs,
    load_snapshot,
    read_snapshot,
    save_output_snapshot,
    save_snapshot,
)


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


def test_identical_outputs_share_body_but_keep_logical_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshot_paths, "SNAPSHOT_DIR", tmp_path)
    bp = snapshot_paths.snapshot_path("bp", 0)
    ap = snapshot_paths.snapshot_path("ap", 3)
    original = Root()
    original.self_ref = original
    with deduplicate_outputs():
        for path, point in [(bp, None), (ap, "ap_0.2")]:
            path.parent.mkdir(parents=True)
            save_output_snapshot(
                original, path, model_version=9, input_point=point, continuation=None
            )
            assert not path.exists()
            assert not (path.parent / "index.json").exists()
        assert snapshot_paths.branch_outputs("bp", 0) == [bp]
        assert load_snapshot(Root(), ap, model_version=9).get("input_point") == "ap_0.2"
        with pytest.raises(SnapshotError, match="refusing to overwrite"):
            save_output_snapshot(
                original, ap, model_version=9, input_point=None, continuation=None
            )

    assert snapshot_paths.branch_outputs("ap", 0) == [ap]
    bodies = list((tmp_path / ".objects").glob("*.snap-body"))
    assert len(bodies) == 1
    for path, point in [(bp, None), (ap, "ap_0.2")]:
        restored = Root(value=0)
        metadata = load_snapshot(
            restored, path, model_version=9, expected_checkpoint=path.stem
        )
        assert restored.value == 3 and restored.self_ref is restored
        assert metadata.get("input_point") == point
        assert bodies[0].stem == metadata["payload_sha256"]
    with pytest.raises(SnapshotCompatibilityError, match="checkpoint mismatch"):
        load_snapshot(Root(), bp, model_version=9, expected_checkpoint=ap.stem)
    with pytest.raises(SnapshotCompatibilityError, match="model_version"):
        load_snapshot(Root(), bp, model_version=8)

    bodies[0].write_bytes(b"damaged")
    restored = Root(value=0)
    with pytest.raises(SnapshotFormatError, match="checksum mismatch"):
        load_snapshot(restored, ap, model_version=9)
    assert restored.value == 0


def test_output_index_is_published_when_derivation_raises(tmp_path: Path):
    output = tmp_path / "bp" / "outputs" / "bp_0.0.snap"
    output.parent.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="contract failed"), deduplicate_outputs():
        save_output_snapshot(
            Root(), output, model_version=9, input_point=None, continuation=None
        )
        raise RuntimeError("contract failed")
    assert load_snapshot(Root(), output, model_version=9)["checkpoint"] == output.stem


def test_break_pack_reads_each_range_in_memory_and_after_publication(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(snapshot_paths, "SNAPSHOT_DIR", tmp_path)
    first = snapshot_paths.snapshot_path("ap", 0)
    second = snapshot_paths.snapshot_path("ap", 1)
    first.parent.mkdir(parents=True)
    pack = first.parent / "breaks.pack"
    with deduplicate_outputs(pack_breaks=True):
        for value, path in enumerate((first, second)):
            root = Root(value=value)
            save_output_snapshot(
                root,
                path,
                model_version=9,
                input_point=f"ap_0.{value}",
                continuation={"saved": root},
            )
            assert not path.exists()
        assert not pack.exists()
        assert snapshot_paths.branch_outputs("ap", 0) == [first, second]
        restored = Root()
        assert read_snapshot(restored, first, model_version=9)[2] == {"saved": restored}

    assert pack.is_file()
    for value, path in enumerate((first, second)):
        restored = Root(value=-1)
        assert (
            load_snapshot(restored, path, model_version=9).get("input_point")
            == f"ap_0.{value}"
        )
        assert restored.value == value

    # Damage the second range without affecting reads of the first entry.
    data = pack.read_bytes()
    pack.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    load_snapshot(Root(), first, model_version=9)
    with pytest.raises(SnapshotFormatError, match="checksum mismatch"):
        load_snapshot(Root(), second, model_version=9)
    pack.write_bytes(data[:-1])
    with pytest.raises(SnapshotFormatError, match="range exceeds file size"):
        load_snapshot(Root(), second, model_version=9)


def test_failed_index_publish_leaves_prior_pack_entries_readable(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshot_paths, "SNAPSHOT_DIR", tmp_path)
    first = snapshot_paths.snapshot_path("ap", 0)
    second = snapshot_paths.snapshot_path("ap", 1)
    first.parent.mkdir(parents=True)
    index = first.parent / "index.json"
    with deduplicate_outputs(pack_breaks=True):
        save_output_snapshot(
            Root(),
            first,
            model_version=9,
            input_point="ap_0.0",
            continuation={"first": True},
        )
    previous = index.read_bytes()
    write_atomic = snapshot._write_atomic

    def fail_index(path, data):
        if path == index:
            raise SnapshotError("index write failed")
        write_atomic(path, data)

    with (
        monkeypatch.context() as patch,
        pytest.raises(SnapshotError, match="index write failed"),
        deduplicate_outputs(pack_breaks=True),
    ):
        patch.setattr(snapshot, "_write_atomic", fail_index)
        save_output_snapshot(
            Root(),
            second,
            model_version=9,
            input_point="ap_0.1",
            continuation={"second": True},
        )
    assert index.read_bytes() == previous
    assert snapshot_paths.branch_outputs("ap", 0) == [first]
    assert read_snapshot(Root(), first, model_version=9)[2] == {"first": True}
