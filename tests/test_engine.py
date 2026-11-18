"""Framework action dispatch and its automatic snapshot boundaries."""

import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

import pytest

from framework import snapshot_paths
from framework.engine import (
    SnapshotCandidate,
    StepPoint,
    System,
    TaskLocalEnv,
    exit_from,
)
from framework.snapshot import SnapshotError, load_snapshot


@dataclass
class ExampleSystem(System):
    value: int = 0
    releases: int = 0
    fail_release: bool = False

    def release(self, env: TaskLocalEnv, action: str):
        self.releases += 1
        if self.fail_release:
            raise RuntimeError("release failed")

    def _step(self, env: TaskLocalEnv, amount: int = 1) -> int:
        self.value += amount
        return self.value

    def public(self, env: TaskLocalEnv) -> int:
        self.value += 1
        return self.value

    def wrapper(self, env: TaskLocalEnv) -> int:
        return self.drive(env, "self", "_step")

    def _nested(self, env: TaskLocalEnv) -> int:
        self.drive(env, "self", "_step")
        self.value += 10
        return self.value

    def _queued(self, env: TaskLocalEnv):
        self.value += 10
        self.emit(env, "self", "_step")

    def _fail(self, env: TaskLocalEnv):
        self.value += 1
        raise RuntimeError("action failed")

    def _nested_fail(self, env: TaskLocalEnv):
        self.drive(env, "self", "_step")
        raise RuntimeError("parent failed")

    def _stop(self, env: TaskLocalEnv):
        exit_from("example", "stopped")


@dataclass
class ExampleRoot:
    system: ExampleSystem = field(default_factory=ExampleSystem)
    self_ref: object | None = None
    unpicklable: object | None = None


@pytest.fixture
def root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ExampleRoot:
    """Supply a framework-only object graph, without importing the kernel model."""
    root = ExampleRoot(system=ExampleSystem())
    module = ModuleType("global_vars")
    vars(module).update(gv=root, MODEL_VERSION=1)
    monkeypatch.setitem(sys.modules, "global_vars", module)
    monkeypatch.setattr(snapshot_paths, "SNAPSHOT_DIR", tmp_path)
    return root


def read_snapshot(tmp_path: Path, checkpoint: str) -> ExampleRoot:
    restored = ExampleRoot()
    load_snapshot(
        restored,
        tmp_path / f"{checkpoint}.snap",
        model_version=1,
        expected_checkpoint=checkpoint,
    )
    return restored


def test_private_actions_snapshot_after_release(root: ExampleRoot, tmp_path: Path):
    env = TaskLocalEnv(name="example")
    root.self_ref = root

    assert root.system.drive(env, "system", "_step", amount=3) == 3
    assert root.system.drive(env, "system", "_step", amount=4) == 7

    first = read_snapshot(tmp_path, "example_0.0")
    second = read_snapshot(tmp_path, "example_1.0")
    assert (first.system.value, first.system.releases) == (3, 1)
    assert (second.system.value, second.system.releases) == (7, 2)
    assert root.system.releases == 2
    assert first.self_ref is first
    assert second.self_ref is second
    assert env.snapshot_position == 2
    assert env.snapshot_branch == 0
    assert env.snapshot_candidates == []
    assert env.range is None
    assert env.depth == 0
    assert not env.signal_queues


def test_public_action_does_not_snapshot(root: ExampleRoot, tmp_path: Path):
    env = TaskLocalEnv(name="example")

    assert root.system.drive(env, "self", "public") == 1

    assert not list(tmp_path.iterdir())
    assert env.snapshot_position == 0


def test_public_wrapper_only_snapshots_its_private_action(
    root: ExampleRoot, tmp_path: Path
):
    env = TaskLocalEnv(name="example")

    assert root.system.drive(env, "self", "wrapper") == 1

    assert read_snapshot(tmp_path, "example_0.0").system.value == 1
    assert env.snapshot_position == 1


def test_nested_actions_numbered_in_return_order(root: ExampleRoot, tmp_path: Path):
    env = TaskLocalEnv(name="example")

    assert root.system.drive(env, "self", "_nested") == 11

    assert read_snapshot(tmp_path, "example_0.0").system.value == 1
    assert read_snapshot(tmp_path, "example_1.0").system.value == 11
    assert env.snapshot_position == 2


def test_emitted_actions_also_snapshot(root: ExampleRoot, tmp_path: Path):
    env = TaskLocalEnv(name="example")

    root.system.drive(env, "self", "_queued")

    assert read_snapshot(tmp_path, "example_0.0").system.value == 10
    assert read_snapshot(tmp_path, "example_1.0").system.value == 11
    assert env.snapshot_position == 2


def test_flows_have_independent_snapshot_positions(root: ExampleRoot, tmp_path: Path):
    first = TaskLocalEnv(name="first")
    second = TaskLocalEnv(name="second")

    root.system.drive(first, "self", "_step")
    root.system.drive(second, "self", "_step")
    root.system.drive(first, "self", "_step")

    assert {path.name for path in tmp_path.iterdir()} == {
        "first_0.0.snap",
        "first_1.0.snap",
        "second_0.0.snap",
    }
    assert (first.snapshot_position, second.snapshot_position) == (2, 1)
    assert first.cv is not second.cv
    assert first.signal_queues is not second.signal_queues
    first.snapshot_candidates.append(SnapshotCandidate(name="source_0.0"))
    first.range = (2, 5)
    assert first.snapshot_candidates is not second.snapshot_candidates
    assert second.snapshot_candidates == []
    assert second.range is None


def test_snapshot_names_include_execution_branch(root: ExampleRoot, tmp_path: Path):
    env = TaskLocalEnv(name="example", snapshot_branch=2)

    root.system.drive(env, "self", "_step")

    assert read_snapshot(tmp_path, "example_0.2").system.value == 1
    assert env.snapshot_branch == 2


def test_action_failure_snapshots_after_release(root: ExampleRoot, tmp_path: Path):
    env = TaskLocalEnv(name="example")

    with pytest.raises(RuntimeError, match="action failed"):
        root.system.drive(env, "self", "_fail")

    assert root.system.releases == 1
    failed = read_snapshot(tmp_path, "example_0.0")
    assert (failed.system.value, failed.system.releases) == (1, 1)
    assert env.snapshot_position == 1
    assert env.depth == 0
    assert not env.signal_queues
    root.system.drive(env, "self", "_step")
    assert read_snapshot(tmp_path, "example_1.0").system.value == 2


def test_parent_failure_preserves_completed_child_snapshot(
    root: ExampleRoot, tmp_path: Path
):
    env = TaskLocalEnv(name="example")

    with pytest.raises(RuntimeError, match="parent failed"):
        root.system.drive(env, "self", "_nested_fail")

    assert read_snapshot(tmp_path, "example_0.0").system.value == 1
    parent = read_snapshot(tmp_path, "example_1.0")
    assert (parent.system.value, parent.system.releases) == (1, 2)
    assert env.snapshot_position == 2
    assert len(list(tmp_path.iterdir())) == 2


def test_step_point_snapshots_after_release(root: ExampleRoot, tmp_path: Path):
    env = TaskLocalEnv(name="example")

    with pytest.raises(StepPoint):
        root.system.drive(env, "self", "_stop")

    assert root.system.releases == 1
    stopped = read_snapshot(tmp_path, "example_0.0")
    assert (stopped.system.value, stopped.system.releases) == (0, 1)
    assert env.snapshot_position == 1


def test_release_failure_skips_snapshot(root: ExampleRoot, tmp_path: Path):
    env = TaskLocalEnv(name="example")
    root.system.fail_release = True

    with pytest.raises(RuntimeError, match="release failed"):
        root.system.drive(env, "self", "_step")

    assert root.system.value == 1
    assert root.system.releases == 1
    assert env.snapshot_position == 0
    assert env.depth == 0
    assert not env.signal_queues
    assert not list(tmp_path.iterdir())


def test_save_failure_preserves_files_and_position(root: ExampleRoot, tmp_path: Path):
    env = TaskLocalEnv(name="example")
    root.system.drive(env, "self", "_step")
    first_path = tmp_path / "example_0.0.snap"
    first_bytes = first_path.read_bytes()
    root.unpicklable = lambda: None

    with pytest.raises(SnapshotError, match="cannot save snapshot"):
        root.system.drive(env, "self", "_step")

    assert first_path.read_bytes() == first_bytes
    assert list(tmp_path.iterdir()) == [first_path]
    assert root.system.releases == 2
    assert env.snapshot_position == 1
    assert env.depth == 0
    assert not env.signal_queues
    del root.unpicklable
    root.system.drive(env, "self", "_step")
    assert read_snapshot(tmp_path, "example_1.0").system.value == 3


def test_save_failure_keeps_original_action_error_in_exception_chain(
    root: ExampleRoot, tmp_path: Path
):
    env = TaskLocalEnv(name="example")
    root.unpicklable = lambda: None

    with pytest.raises(SnapshotError, match="cannot save snapshot") as error:
        root.system.drive(env, "self", "_fail")

    original: BaseException = error.value
    while original.__context__ is not None:
        original = original.__context__
    assert isinstance(original, RuntimeError)
    assert str(original) == "action failed"
    assert root.system.releases == 1
    assert env.snapshot_position == 0
    assert env.depth == 0
    assert not env.signal_queues
    assert not list(tmp_path.iterdir())


def test_save_failure_in_an_outer_except_still_propagates(root: ExampleRoot):
    env = TaskLocalEnv(name="example")
    root.unpicklable = lambda: None

    try:
        raise RuntimeError("outer error")
    except RuntimeError:
        with pytest.raises(SnapshotError, match="cannot save snapshot"):
            root.system.drive(env, "self", "_step")

    assert root.system.releases == 1
    assert env.snapshot_position == 0
