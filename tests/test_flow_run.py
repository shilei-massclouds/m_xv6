"""Spec source operations, durable I/O records and first-run isolation."""

import json
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from framework import flow_run, snapshot_paths
from framework.contracts import ContractViolation
from framework.engine import StepPoint, System, TaskLocalEnv
from framework.flow_run import FlowRun, require_run
from framework.input_encoding import InputEncodingError
from framework.snapshot import SnapshotError, load_snapshot
from framework.spec import SpecRegistry


@dataclass
class NativeTarget(System):
    releases: int = 0

    def _calculate(self, env):
        return 7

    def change_branch(self, env):
        run = require_run(env)
        run.branch = 4
        run.input_position = 0
        run.output_position = 0

    def release(self, env, action):
        self.releases += 1


@dataclass
class Root:
    worker: Any = None
    native: NativeTarget = field(default_factory=NativeTarget)
    bad: Any = None


@pytest.fixture
def root(monkeypatch, tmp_path):
    root = Root()
    module = ModuleType("global_vars")
    vars(module).update(gv=root, MODEL_VERSION=1)
    monkeypatch.setitem(sys.modules, "global_vars", module)
    monkeypatch.setattr(snapshot_paths, "SNAPSHOT_DIR", tmp_path)
    return root


def model(root: Root, tmp_path: Path, source: str):
    path = tmp_path / "IoModel.spec"
    path.write_text(textwrap.dedent(source).lstrip())
    root.worker = SpecRegistry([path]).model("IoModel")()
    return path


def points(flow="example"):
    return json.loads(snapshot_paths.input_records_path(flow).read_text())["points"]


def output(position, flow="example", branch=0):
    restored = Root()
    path = snapshot_paths.snapshot_path(flow, position, branch)
    metadata = load_snapshot(
        restored, path, model_version=1, expected_checkpoint=path.stem
    )
    return restored, metadata


@pytest.mark.parametrize("action", ["start", "_start"])
def test_only_actions_operations_create_points_and_nested_drives_complete_in_order(
    root, tmp_path, action
):
    path = model(
        root,
        tmp_path,
        f"""
        Prop value: int = 0

        Def {action}():
            Actions:
                self.value = 1
                with GuardSpinLock(env.cv, self):
                    assert self.value == 1
                    drive("self", "empty")
                    self.value = drive("self", "_value")
                pass
            Ensures:
                self.value == 7 and env.cv.local_irq == 0

        Def empty():
            Actions:
                pass

        Def _value() -> result:
            Actions:
                result = 7
    """,
    )
    env = TaskLocalEnv(name="example")
    with FlowRun(root, env, 1) as run:
        System().drive(env, "worker", action)

    records = points()
    assert [point["operation"]["text"] for point in records] == [
        "self.value = 1",
        'drive("self", "empty")',
        'self.value = drive("self", "_value")',
        "result = 7",
    ]
    assert [point["name"] for point in records] == [f"example_0.{i}" for i in range(4)]
    assert [point["input_snapshot"] for point in records] == [
        "example/outputs/example_0.0.snap",
        "example/outputs/example_0.1.snap",
        "example/outputs/example_0.2.snap",
        "example/outputs/example_0.2.snap",
    ]
    for point in records:
        assert point["base"] is None
        assert (
            path.read_text().splitlines()[point["location"]["line"] - 1].strip()
            == point["operation"]["text"]
        )
    states = [output(i) for i in range(5)]
    assert [state.worker.value for state, _ in states] == [0, 1, 1, 1, 7]
    assert [meta.get("input_point") for _, meta in states] == [
        None,
        "example_0.0",
        "example_0.1",
        "example_0.3",
        "example_0.2",
    ]
    assert (run.input_position, run.output_position) == (4, 5)
    assert env.depth == 0 and not env.signal_queues
    assert all(stack == [None] for stack in env.cv.stacks.values())


def test_native_target_has_only_its_calling_spec_line_points(root, tmp_path):
    model(
        root,
        tmp_path,
        """
        Prop value: int = 0
        Def start():
            Actions:
                self.value = drive("native", "_calculate")
    """,
    )
    env = TaskLocalEnv(name="example")
    with FlowRun(root, env, 1):
        System().drive(env, "worker", "start")
    assert len(points()) == 1
    restored, meta = output(1)
    assert restored.worker.value == 7
    assert restored.native.releases == 1
    assert meta.get("input_point") == "example_0.0"


@pytest.mark.parametrize(
    "failure", ['exit_from("blocked", "BUSY_CHECK")', 'assert False, "blocked"']
)
def test_failed_operations_have_no_output_and_completed_child_outputs_survive(
    root, tmp_path, failure
):
    model(
        root,
        tmp_path,
        f"""
        Prop value: int = 0
        Def start():
            Actions:
                with GuardSpinLock(env.cv, self):
                    drive("self", "_child")
        Def _child():
            Actions:
                self.value = 1
                {failure}
    """,
    )
    env = TaskLocalEnv(name="example")
    error = StepPoint if failure.startswith("exit_from") else AssertionError
    with pytest.raises(error), FlowRun(root, env, 1) as run:
        System().drive(env, "worker", "start")
    assert run.failed
    assert len(points()) == (3 if error is StepPoint else 2)
    assert run.output_position == 2
    restored, meta = output(1)
    assert restored.worker.value == 1
    assert meta.get("input_point") == "example_0.1"
    assert not snapshot_paths.snapshot_path("example", 2).exists()
    assert env.depth == 0 and not env.signal_queues
    assert all(stack == [None] for stack in env.cv.stacks.values())


def test_failed_callee_ensures_does_not_complete_calling_drive(root, tmp_path):
    model(
        root,
        tmp_path,
        """
        Prop value: int = 0
        Def start():
            Actions:
                drive("self", "_child")
        Def _child():
            Actions:
                self.value = 1
            Ensures:
                False
    """,
    )
    env = TaskLocalEnv(name="example")
    with pytest.raises(ContractViolation), FlowRun(root, env, 1) as run:
        System().drive(env, "worker", "start")
    assert len(points()) == 2
    assert run.output_position == 2
    assert output(1)[1].get("input_point") == "example_0.1"


def test_release_failure_does_not_complete_calling_drive(root, tmp_path, monkeypatch):
    model(
        root,
        tmp_path,
        """
        Def start():
            Actions:
                with GuardSpinLock(env.cv, self):
                    drive("self", "_child")
        Def _child():
            Actions:
                pass
    """,
    )

    def release(self, env, action):
        if action == "_child":
            raise RuntimeError("release failed")

    monkeypatch.setattr(type(root.worker), "release", release)
    env = TaskLocalEnv(name="example")
    with (
        pytest.raises(RuntimeError, match="release failed"),
        FlowRun(root, env, 1) as run,
    ):
        System().drive(env, "worker", "start")
    assert len(points()) == 1
    assert run.output_position == 1
    assert env.depth == 0 and not env.signal_queues
    assert all(stack == [None] for stack in env.cv.stacks.values())


def test_output_uses_current_branch_instead_of_call_entry_branch(root, tmp_path):
    model(
        root,
        tmp_path,
        """
        Def start():
            Actions:
                drive("native", "change_branch")
    """,
    )
    env = TaskLocalEnv(name="example")
    with FlowRun(root, env, 1):
        System().drive(env, "worker", "start")
    assert points()[0]["name"] == "example_0.0"
    assert output(0, branch=4)[1].get("input_point") == "example_0.0"


def test_record_write_failure_stops_before_operation_and_preserves_files(
    root, tmp_path, monkeypatch
):
    model(
        root,
        tmp_path,
        """
        Prop value: int = 0
        Def increment():
            Actions:
                self.value += 1
    """,
    )
    env = TaskLocalEnv(name="example")
    with FlowRun(root, env, 1) as run:
        System().drive(env, "worker", "increment")
        record_path = snapshot_paths.input_records_path("example")
        original_records = record_path.read_bytes()
        original_output = snapshot_paths.snapshot_path("example", 1).read_bytes()
        replace = flow_run.os.replace

        def fail_replace(source, destination):
            if destination == record_path:
                raise OSError("record write failed")
            return replace(source, destination)

        monkeypatch.setattr(flow_run.os, "replace", fail_replace)
        with pytest.raises(SnapshotError, match="record write failed"):
            System().drive(env, "worker", "increment")
        assert root.worker.value == 1
        assert (run.input_position, run.output_position) == (1, 2)
        assert record_path.read_bytes() == original_records
        assert (
            snapshot_paths.snapshot_path("example", 1).read_bytes() == original_output
        )
        assert not list(tmp_path.rglob("*.tmp"))
        with pytest.raises(RuntimeError, match="has failed"):
            System().drive(env, "worker", "increment")


def test_output_save_failure_preserves_input_and_last_successful_output(root, tmp_path):
    model(
        root,
        tmp_path,
        """
        Prop value: int = 0
        Def increment():
            Actions:
                self.value += 1
    """,
    )
    env = TaskLocalEnv(name="example")
    with FlowRun(root, env, 1) as run:
        System().drive(env, "worker", "increment")
        path = snapshot_paths.snapshot_path("example", 1)
        original = path.read_bytes()
        root.bad = lambda: None
        with pytest.raises(SnapshotError, match="cannot save snapshot"):
            System().drive(env, "worker", "increment")
        assert (run.input_position, run.output_position) == (2, 2)
        assert run.latest_output == "example/outputs/example_0.1.snap"
        assert path.read_bytes() == original
        assert len(points()) == 2
        assert root.worker.value == 2
        assert not snapshot_paths.snapshot_path("example", 2).exists()
        assert not list(tmp_path.rglob("*.tmp"))


def test_existing_first_run_is_never_overwritten(root, tmp_path):
    env = TaskLocalEnv(name="example")
    with FlowRun(root, env, 1):
        pass
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    with (
        pytest.raises(SnapshotError, match="already has records"),
        FlowRun(root, TaskLocalEnv(name="example"), 1),
    ):
        pytest.fail("existing first-run data must be retained")
    assert {path: path.read_bytes() for path in before} == before


def test_failed_initial_save_leaves_no_active_context(root):
    root.bad = lambda: None
    with pytest.raises(SnapshotError), FlowRun(root, TaskLocalEnv(name="broken"), 1):
        pytest.fail("unpicklable initial GV")
    with pytest.raises(RuntimeError, match="active FlowRun"):
        require_run(TaskLocalEnv(name="broken"))
    root.bad = None
    with FlowRun(root, TaskLocalEnv(name="other"), 1):
        pass


def test_context_and_environment_are_required(root, tmp_path):
    model(
        root,
        tmp_path,
        """
        Def start():
            Actions:
                pass
    """,
    )
    env = TaskLocalEnv(name="example")
    with pytest.raises(RuntimeError, match="active FlowRun"):
        System().drive(env, "worker", "start")
    with FlowRun(root, env, 1):
        with pytest.raises(RuntimeError, match="environment"):
            System().drive(TaskLocalEnv(name="example"), "worker", "start")
        with (
            pytest.raises(RuntimeError, match="nested"),
            FlowRun(root, TaskLocalEnv(name="nested"), 1),
        ):
            pass
    assert points() == []


def test_unsupported_input_fails_before_recording_or_handling_call(root, tmp_path):
    model(
        root,
        tmp_path,
        """
        Prop value: int = 0
        Def start(value):
            Actions:
                drive("self", "consume", value=value)
        Def consume(value):
            Actions:
                self.value = 1
    """,
    )
    env = TaskLocalEnv(name="example")
    with pytest.raises(InputEncodingError), FlowRun(root, env, 1) as run:
        System().drive(env, "worker", "start", value=object())
    assert root.worker.value == 0
    assert points() == []
    assert (run.input_position, run.output_position) == (0, 1)
