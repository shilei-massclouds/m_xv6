"""Snapshots preserve object graphs and commit only after successful I/O."""

import hashlib
import json
import pickle
import subprocess
import sys
from collections import deque
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import MethodType
from typing import Any

import pytest

from framework import snapshot
from framework.engine import Signal, TaskLocalEnv
from global_vars import MODEL_VERSION, GlobalVars, gv
from kernel.proc import ProcState
from origins.bp_init_origin import BpInitOrigin

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def model() -> Iterator[Any]:
    previous_state = gv.__dict__
    gv.__dict__ = vars(GlobalVars())
    try:
        yield gv
    finally:
        gv.__dict__ = previous_state


def _marker(owner: Any) -> object:
    return owner.marker


def _rewrite_header(path: Path, **changes: object) -> None:
    magic, header, payload = path.read_bytes().split(b"\n", 2)
    metadata = json.loads(header)
    metadata.update(changes)
    path.write_bytes(magic + b"\n" + json.dumps(metadata).encode() + b"\n" + payload)


def _rewrite_payload(path: Path, payload: bytes) -> None:
    magic, header, _ = path.read_bytes().split(b"\n", 2)
    metadata = json.loads(header)
    metadata["payload_sha256"] = hashlib.sha256(payload).hexdigest()
    path.write_bytes(magic + b"\n" + json.dumps(metadata).encode() + b"\n\n" + payload)


def test_boot_state_and_header_round_trip(model: Any, tmp_path: Path) -> None:
    BpInitOrigin().start()
    model.initproc.pid = 42
    original_root = model
    original_proc = model.initproc
    path = tmp_path / "boot.snap"
    before = datetime.now(UTC)

    saved = model.save_snapshot(path, checkpoint="已初始化")

    after = datetime.now(UTC)
    with path.open("rb") as stream:
        assert stream.readline() == snapshot.MAGIC
        assert json.loads(stream.readline().decode("utf-8")) == saved
        assert stream.readline() == b"\n"
        assert hashlib.sha256(stream.read()).hexdigest() == saved["payload_sha256"]
    assert saved["format_version"] == snapshot.FORMAT_VERSION
    assert saved["model_version"] == MODEL_VERSION
    assert saved["checkpoint"] == "已初始化"
    created_at = datetime.fromisoformat(saved["created_at"])
    assert created_at.utcoffset() == UTC.utcoffset(None)
    assert before <= created_at <= after

    model.reset()
    assert model.initproc.state is ProcState.UNUSED
    loaded = model.load_snapshot(path, expected_checkpoint="已初始化")

    assert loaded == saved
    assert model is gv is original_root
    assert model.initproc is not original_proc
    assert model.initproc.state is ProcState.RUNNABLE
    assert model.initproc.pid == 42
    assert model.childproc.state is ProcState.UNUSED


def test_dynamic_attributes_are_restored_and_later_attributes_removed(
    model: Any, tmp_path: Path
) -> None:
    path = tmp_path / "dynamic.snap"
    model.marker = {
        "sequence": [1, 2],
        "states": {ProcState.UNUSED, ProcState.RUNNABLE},
    }
    model.initproc.extra = ("nested", 3)
    model.save_snapshot(path, checkpoint="dynamic")
    del model.marker
    del model.initproc.extra
    model.later = "remove me"
    model.initproc.later = True

    model.load_snapshot(path)

    assert model.marker == {
        "sequence": [1, 2],
        "states": {ProcState.UNUSED, ProcState.RUNNABLE},
    }
    assert model.initproc.extra == ("nested", 3)
    assert not hasattr(model, "later")
    assert not hasattr(model.initproc, "later")


def test_aliases_cycles_and_root_references(model: Any, tmp_path: Path) -> None:
    path = tmp_path / "graph.snap"
    shared: list[object] = []
    shared.extend([shared, model, model.initproc])
    model.shared = shared
    model.alias = {"same": shared, "root": model, "attrs": vars(model)}
    model.self = model
    model.initproc.owner = model
    model.initproc.peer = model.childproc
    model.childproc.peer = model.initproc
    model.save_snapshot(path, checkpoint="graph")

    model.load_snapshot(path)

    assert model.self is model
    assert model.shared is model.alias["same"]
    assert model.shared is not shared
    assert model.shared[0] is model.shared
    assert model.shared[1] is model.alias["root"] is gv
    assert model.shared[2] is model.initproc
    assert model.alias["attrs"] is model.__dict__
    assert model.initproc.owner is model
    assert model.initproc.peer is model.childproc
    assert model.childproc.peer is model.initproc


def test_environment_queues_and_protection_aliases(model: Any, tmp_path: Path) -> None:
    path = tmp_path / "environment.snap"
    env = TaskLocalEnv()
    queue: deque[Signal] = deque()
    env.signal_queues.append(queue)
    queue.append(
        Signal(
            source=model.bp_init_flow,
            target="initproc",
            action="_preset",
            args={"root": model},
            env=env,
            queue=queue,
        )
    )
    queue.append(
        Signal(
            source=model.initproc,
            target="self",
            action="_preset",
            args={},
            env=env,
            queue=queue,
        )
    )
    env.cv.protect(model.initproc, "local_irq")
    model.environment = env
    model.save_snapshot(path, checkpoint="queued")

    model.load_snapshot(path)

    restored = model.environment
    signal = restored.signal_queues[0][0]
    assert restored is not env
    assert signal.env is restored
    assert signal.queue is restored.signal_queues[0]
    assert signal.args["root"] is model
    assert signal.source is model.bp_init_flow
    assert restored.cv.stacks["local_irq"][-1] is model.initproc
    self_signal = restored.signal_queues[0][1]
    assert self_signal.source is model.initproc
    assert self_signal.source is not env.signal_queues[0][1].source
    assert self_signal.env is restored
    assert self_signal.queue is restored.signal_queues[0]
    assert self_signal.target == "self"
    assert self_signal.handle() is None


def test_bound_methods_keep_their_function_owner_and_aliases(
    model: Any, tmp_path: Path
) -> None:
    path = tmp_path / "methods.snap"
    model.marker = "root"
    model.initproc.marker = "process"
    model.root_method = model.reset
    model.proc_method = model.initproc._preset
    model.dynamic_method = MethodType(_marker, model)
    model.initproc.dynamic_method = MethodType(_marker, model.initproc)
    model.method_alias = model.dynamic_method
    model.save_snapshot(path, checkpoint="methods")
    model.marker = "changed"

    model.load_snapshot(path)

    assert model.root_method.__self__ is model
    assert model.root_method.__func__ is GlobalVars.reset
    assert model.proc_method.__self__ is model.initproc
    assert model.proc_method.__func__ is type(model.initproc)._preset
    assert model.dynamic_method is model.method_alias
    assert model.dynamic_method() == "root"
    assert model.initproc.dynamic_method() == "process"
    model.proc_method(TaskLocalEnv())


def test_separate_roots_and_snapshot_files_are_independent(
    model: Any, tmp_path: Path
) -> None:
    first = tmp_path / "first.snap"
    second = tmp_path / "second.snap"
    model.values = [1]
    model.self = model
    model.save_snapshot(first, checkpoint="first")
    first_bytes = first.read_bytes()
    model.values.append(2)
    model.save_snapshot(second, checkpoint="second")

    other: Any = GlobalVars()
    other.load_snapshot(first)
    assert other is not model
    assert other.self is other
    assert other.values == [1]
    assert model.values == [1, 2]
    model.load_snapshot(first)
    model.values.append(99)
    model.load_snapshot(first)
    assert model.values == [1]
    model.load_snapshot(second, expected_checkpoint="second")
    assert model.values == [1, 2]
    assert first.read_bytes() == first_bytes


def test_save_replaces_an_existing_file(model: Any, tmp_path: Path) -> None:
    path = tmp_path / "replace.snap"
    model.marker = "first"
    model.save_snapshot(path, checkpoint="first")
    model.marker = "second"
    metadata = model.save_snapshot(path, checkpoint="second")
    del model.marker

    assert model.load_snapshot(path, expected_checkpoint="second") == metadata
    assert model.marker == "second"
    assert list(tmp_path.iterdir()) == [path]


def test_snapshot_loads_in_a_fresh_process(tmp_path: Path) -> None:
    path = tmp_path / "process.snap"
    save = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from global_vars import gv
from kernel.proc import ProcState
gv.initproc.state = ProcState.RUNNABLE
gv.initproc.pid = 73
gv.shared = [gv.initproc, gv]
gv.alias = gv.shared
gv.root_method = gv.reset
gv.proc_method = gv.initproc._preset
gv.save_snapshot(sys.argv[1], checkpoint='process')
""",
            str(path),
        ],
        check=False,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert save.returncode == 0, save.stderr
    load = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from global_vars import gv
from kernel.proc import ProcState
original_root = gv
original_proc = gv.initproc
assert gv.initproc.state is ProcState.UNUSED
metadata = gv.load_snapshot(sys.argv[1], expected_checkpoint='process')
assert metadata['checkpoint'] == 'process'
assert gv is original_root
assert gv.initproc is not original_proc
assert gv.initproc.state is ProcState.RUNNABLE
assert gv.initproc.pid == 73
assert gv.shared is gv.alias
assert gv.shared[0] is gv.initproc
assert gv.shared[1] is gv
assert gv.root_method.__self__ is gv
assert gv.proc_method.__self__ is gv.initproc
print('snapshot restored')
""",
            str(path),
        ],
        check=False,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert load.returncode == 0, load.stderr
    assert load.stdout.strip() == "snapshot restored"


@pytest.mark.parametrize(
    ("changes", "expected_checkpoint", "message"),
    [
        ({"format_version": 999}, None, "format_version"),
        ({"model_version": 3}, None, "model_version"),
        ({"model_version": MODEL_VERSION + 1}, None, "model_version"),
        ({}, "wrong", "checkpoint mismatch"),
    ],
)
def test_incompatible_snapshots_preserve_live_state(
    model: Any,
    tmp_path: Path,
    changes: dict[str, object],
    expected_checkpoint: str | None,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "incompatible.snap"
    model.save_snapshot(path, checkpoint="saved")
    _rewrite_header(path, **changes)
    model.marker = ["live"]
    current_state = vars(model)
    current_proc = model.initproc

    def no_deserialization(*args: Any) -> None:
        pytest.fail("incompatible snapshots must be rejected before deserialization")

    monkeypatch.setattr(snapshot, "_SnapshotUnpickler", no_deserialization)
    with pytest.raises(snapshot.SnapshotCompatibilityError, match=message):
        model.load_snapshot(path, expected_checkpoint=expected_checkpoint)

    assert vars(model) is current_state
    assert model.initproc is current_proc
    assert model.marker == ["live"]


@pytest.mark.parametrize(
    ("header", "message"),
    [
        (b"\xff\n", "UTF-8 JSON"),
        (b"{\n", "UTF-8 JSON"),
        (b"[]\n", "JSON object"),
        (b"{}\n", "format_version"),
        (b"{}", "missing or oversized"),
        (b" " * (64 * 1024) + b"\n", "oversized"),
    ],
)
def test_invalid_headers_preserve_live_state(
    model: Any, tmp_path: Path, header: bytes, message: str
) -> None:
    path = tmp_path / "header.snap"
    path.write_bytes(snapshot.MAGIC + header)
    current_state = vars(model)

    with pytest.raises(snapshot.SnapshotFormatError, match=message):
        model.load_snapshot(path)

    assert vars(model) is current_state


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"format_version": True}, "integer format_version"),
        ({"model_version": "1"}, "integer model_version"),
        ({"checkpoint": ""}, "nonempty checkpoint"),
        ({"created_at": "invalid"}, "creation time"),
        ({"created_at": "2026-01-01T00:00:00"}, "must be in UTC"),
        ({"created_at": "2026-01-01T00:00:00+01:00"}, "must be in UTC"),
        ({"payload_sha256": "not a checksum"}, "payload checksum"),
    ],
)
def test_malformed_metadata_preserves_live_state(
    model: Any, tmp_path: Path, changes: dict[str, object], message: str
) -> None:
    path = tmp_path / "metadata.snap"
    model.save_snapshot(path, checkpoint="saved")
    _rewrite_header(path, **changes)
    current_state = vars(model)

    with pytest.raises(snapshot.SnapshotFormatError, match=message):
        model.load_snapshot(path)

    assert vars(model) is current_state


@pytest.mark.parametrize(
    "damage", ["missing", "magic", "separator", "truncate", "flip"]
)
def test_missing_or_damaged_files_preserve_live_state(
    model: Any, tmp_path: Path, damage: str
) -> None:
    path = tmp_path / "damaged.snap"
    model.save_snapshot(path, checkpoint="saved")
    data = path.read_bytes()
    if damage == "missing":
        path.unlink()
    elif damage == "magic":
        path.write_bytes(b"INVALID\n" + data)
    elif damage == "separator":
        path.write_bytes(data.replace(b"\n\n", b"\n", 1))
    elif damage == "truncate":
        path.write_bytes(data[:-8])
    else:
        path.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    model.marker = ["live"]
    current_state = vars(model)
    current_proc = model.initproc

    with pytest.raises(snapshot.SnapshotError, match=str(path)):
        model.load_snapshot(path)

    assert vars(model) is current_state
    assert model.initproc is current_proc
    assert model.marker == ["live"]


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"", "cannot deserialize"),
        (pickle.dumps({"marker": "saved"})[:-1], "cannot deserialize"),
        (pickle.dumps([1, 2]), "instance dictionary"),
        (pickle.dumps({1: "invalid attribute"}), "instance dictionary"),
        (pickle.dumps({"marker": "saved"}) + b"extra", "unexpected data"),
        (b"Psnapshot_unknown\n.", "unknown persistent reference"),
    ],
)
def test_invalid_payloads_preserve_live_state_after_checksum_validation(
    model: Any, tmp_path: Path, payload: bytes, message: str
) -> None:
    path = tmp_path / "payload.snap"
    model.save_snapshot(path, checkpoint="saved")
    _rewrite_payload(path, payload)
    model.marker = "live"
    current_state = vars(model)
    current_proc = model.initproc

    with pytest.raises(snapshot.SnapshotFormatError, match=message):
        model.load_snapshot(path)

    assert vars(model) is current_state
    assert model.initproc is current_proc
    assert model.marker == "live"


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("failure", ["serialize", "fsync", "replace"])
def test_failed_save_preserves_existing_file_and_cleans_temporary_files(
    model: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing: bool,
    failure: str,
) -> None:
    path = tmp_path / "atomic.snap"
    if existing:
        model.marker = "saved"
        model.save_snapshot(path, checkpoint="saved")
    previous_bytes = path.read_bytes() if existing else None
    model.marker = "live"
    if failure == "serialize":
        model.unpicklable = lambda: None
    else:

        def fail(*args: Any) -> None:
            raise OSError(f"injected {failure} failure")

        monkeypatch.setattr(snapshot.os, failure, fail)
    current_state = vars(model)

    with pytest.raises(snapshot.SnapshotError, match="cannot save snapshot"):
        model.save_snapshot(path, checkpoint="replacement")

    assert vars(model) is current_state
    assert model.marker == "live"
    assert sorted(tmp_path.iterdir()) == ([path] if existing else [])
    if existing:
        assert path.read_bytes() == previous_bytes
        model.load_snapshot(path, expected_checkpoint="saved")
        assert model.marker == "saved"


def test_invalid_destination_reports_an_error(model: Any, tmp_path: Path) -> None:
    current_state = vars(model)

    with pytest.raises(snapshot.SnapshotError, match="cannot save snapshot"):
        model.save_snapshot(tmp_path / "missing" / "file.snap", checkpoint="saved")

    assert vars(model) is current_state
    assert list(tmp_path.iterdir()) == []


def test_oversized_header_does_not_replace_existing_file(
    model: Any, tmp_path: Path
) -> None:
    path = tmp_path / "header-size.snap"
    model.save_snapshot(path, checkpoint="saved")
    previous_bytes = path.read_bytes()

    with pytest.raises(snapshot.SnapshotError, match="oversized snapshot header"):
        model.save_snapshot(path, checkpoint="x" * (64 * 1024))

    assert path.read_bytes() == previous_bytes
    assert list(tmp_path.iterdir()) == [path]
