"""Spec source operations, durable I/O records and first-run isolation."""

import json
import sys
import textwrap
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from framework import flow_run, snapshot_paths
from framework.contracts import ContractViolation
from framework.derivation import derive_flows
from framework.engine import FlowPaused, StepPoint, System, TaskLocalEnv
from framework.flow_run import CapturedInput, FlowRun, buffer_input_records, require_run
from framework.input_encoding import InputEncodingError
from framework.interpreter import Continuation, fork_input, resume_checkpoint
from framework.snapshot import (
    SnapshotCompatibilityError,
    SnapshotError,
    SnapshotFormatError,
    deduplicate_outputs,
    load_snapshot,
    read_snapshot,
    save_snapshot,
)
from framework.spec import SpecError, SpecRegistry


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
class DerivationOrigin(System):
    env: TaskLocalEnv
    delta: int

    def start(self) -> None:
        self.drive(self.env, "worker", "start", delta=self.delta)


@dataclass
class ActionOrigin(System):
    env: TaskLocalEnv
    target: str
    action: str

    def start(self) -> None:
        self.drive(self.env, self.target, self.action)


@dataclass
class Root:
    worker: Any = None
    native: NativeTarget = field(default_factory=NativeTarget)
    bad: Any = None
    other: Any = None
    smp_started: Any = None
    other_wait: Any = None


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


def cpu_model(root: Root, tmp_path: Path, actions: str) -> Path:
    path = tmp_path / "CpuFlow.spec"
    path.write_text(
        """Enum CpuFlowState:
    RUNNING
    BUSYCHECK
Prop state: CpuFlowState = CpuFlowState.RUNNING
Prop result: int = 0
Prop history: list[int]
"""
        + textwrap.dedent(actions)
    )
    repo = Path(__file__).resolve().parents[1]
    registry = SpecRegistry(
        [path, repo / "framework/BusyWait.spec", repo / "kernel/SmpStarted.spec"]
    )
    root.worker = registry.model("CpuFlow")()
    root.other = registry.model("CpuFlow")()
    root.smp_started = registry.model("SmpStarted")()
    return path


def start_with_break(root: Root) -> Path:
    with (
        pytest.raises(FlowPaused) as pause,
        FlowRun(root, TaskLocalEnv(name="example"), 1) as run,
    ):
        System().drive(run.env, "worker", "start")
    assert not run.failed
    return pause.value.checkpoint


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
def test_action_points_follow_the_declaring_method_and_drive_completion_order(
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
                assert self.value == 1
                with GuardSpinLock(env.cv, self):
                    drive("self", "observe")
                with GuardSpinLock(env.cv, self):
                    drive("self", "_set_value")
                pass
            Ensures:
                self.value == 7 and env.cv.local_irq == 0

        Def observe():
            Actions:
                self.value += 1

        Def _set_value():
            Actions:
                self.value = 7
    """,
    )
    env = TaskLocalEnv(name="example")
    captured: dict[str, CapturedInput] = {}
    with FlowRun(
        root, env, 1, capture_input=captured.__setitem__, capture_all_inputs=True
    ) as run:
        System().drive(env, "worker", action)

    records = points()
    if action == "_start":
        operations = ["self.value += 1"]
        input_positions = [0]
        values = [0, 2]
        origins = [None, "example_0.0"]
    else:
        operations = [
            "self.value = 1",
            "with GuardSpinLock(env.cv, self):",
            "self.value += 1",
            "with GuardSpinLock(env.cv, self):",
        ]
        input_positions = [0, 1, 1, 3]
        values = [0, 1, 2, 2, 7]
        origins = [None, "example_0.0", "example_0.2", "example_0.1", "example_0.3"]
    names = [f"example_0.{i}" for i in range(len(operations))]
    assert [point["operation"]["text"] for point in records] == operations
    assert [point["name"] for point in records] == list(captured) == names
    assert [point["input_snapshot"] for point in records] == [
        f"example/outputs/example_0.{i}.snap" for i in input_positions
    ]
    for point in records:
        assert point["base"] is None
        assert (
            path.read_text().splitlines()[point["location"]["line"] - 1].strip()
            == point["operation"]["text"]
        )
    states = [output(i) for i in range(len(values))]
    assert [state.worker.value for state, _ in states] == values
    assert [meta.get("input_point") for _, meta in states] == origins
    assert (run.input_position, run.output_position) == (len(operations), len(values))
    assert root.worker.value == 7
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
                    drive("self", "child")
        Def child():
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
                drive("self", "_outer")
        Def _outer():
            Actions:
                with GuardSpinLock(env.cv, self):
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
    assert [point["operation"]["text"] for point in points()] == [
        'drive("self", "_outer")'
    ]
    assert run.output_position == 1
    assert root.worker.value == 1
    assert env.depth == 0 and not env.signal_queues
    assert all(stack == [None] for stack in env.cv.stacks.values())


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


def test_emit_drains_fifo_before_returning_first_result(root, tmp_path):
    model(
        root,
        tmp_path,
        """
        Prop result: int = 0
        Prop history: list[int]
        Def start():
            Actions:
                self.result = drive("self", "first")
                self.history.append(5)
        Def first() -> result:
            Actions:
                emit("self", "second")
                emit("self", "record", value=3)
                self.history.append(1)
                result = 7
        Def second() -> result:
            Actions:
                emit("self", "record", value=4)
                self.history.append(2)
                result = 8
        Def record(value):
            Actions:
                self.history.append(value)
        """,
    )
    with FlowRun(root, TaskLocalEnv(name="example"), 1) as run:
        System().drive(run.env, "worker", "start")
    assert root.worker.result == 7
    assert root.worker.history == [1, 2, 3, 4, 5]
    records = points()
    assert [r["operation"]["kind"] for r in records].count("emit") == 3
    before_return, meta = output(run.output_position - 2)
    assert before_return.worker.history == [1, 2, 3, 4]
    assert before_return.worker.result == 7
    assert meta.get("input_point") == records[0]["name"]


@pytest.mark.parametrize("accept", [True, False])
def test_break_uses_innermost_contract_of_its_owner(root, tmp_path, accept):
    cpu_model(
        root,
        tmp_path,
        f"""
        Def start():
            Actions:
                drive("self", "scope")
                self.history.append(1)
            Ensures:
                self.history == [1]
        Def scope():
            Actions:
                drive("other", "relay")
            Ensures:
                {("self.state is CpuFlowState.BUSYCHECK" if accept else "False")}
        Def relay():
            Actions:
                drive("smp_started", "wait")
            Ensures:
                gv.smp_started.signaled
        """,
    )
    if not accept:
        with (
            pytest.raises(ContractViolation, match=r"ensures failed.*CpuFlow.scope"),
            FlowRun(root, TaskLocalEnv(name="example"), 1) as run,
        ):
            System().drive(run.env, "worker", "start")
        assert run.failed
        assert run.output_position == 1
        assert points()[-1]["operation"]["kind"] == "break"
        assert not run.env.signal_queues
        return

    path = start_with_break(root)
    meta, state, saved = read_snapshot(root, path, model_version=1)
    assert isinstance(saved, Continuation)
    assert saved.owner is state["worker"]
    assert state["worker"].state.name == "BUSYCHECK"
    assert state["other"].state.name == "RUNNING"
    assert state["worker"].history == []
    assert meta.get("input_point") == points()[-1]["name"]
    assert saved.history.pending == {"example_0.0", "example_0.1", "example_0.2"}
    assert saved.restart is not None
    assert saved.restart.system is state["smp_started"]
    assert saved.restart.signal.action == "wait"
    assert saved.drives[-1].queue[0] is saved.restart.signal
    assert not any(frame.guards for frame in saved.frames)
    assert all(stack == [None] for stack in saved.env.cv.stacks.values())
    loaded, _ = output(1)
    assert loaded.worker.state.name == "BUSYCHECK"


def test_wait_resume_preserves_depth_and_finishes_after_notification(
    root, tmp_path, monkeypatch
):
    cpu_model(
        root,
        tmp_path,
        """
        Def start():
            Requires:
                self.state is CpuFlowState.RUNNING
            Actions:
                drive("smp_started", "wait")
                self.state = CpuFlowState.RUNNING
                self.history.append(1)
            Ensures:
                self.state is CpuFlowState.BUSYCHECK
                self.history == [1]
        """,
    )
    path = start_with_break(root)
    initial_path, initial_data = path, path.read_bytes()
    for attempt in range(1, 4):
        with pytest.raises(FlowPaused) as pause:
            resume_checkpoint(root, path, 1)
        path = pause.value.checkpoint
        _, _, saved = read_snapshot(root, path, model_version=1)
        assert isinstance(saved, Continuation)
        assert len(saved.frames) == 1
        assert saved.env.depth == 2
        assert saved.history.pending == {"example_0.0"}
        assert saved.history.output_position == 2 + attempt
        assert [r["operation"]["kind"] for r in points()[-2:]] == ["with", "break"]
        assert all(stack == [None] for stack in saved.env.cv.stacks.values())
        assert root.worker.history == []
    assert initial_path.read_bytes() == initial_data
    with pytest.raises(SnapshotCompatibilityError, match="latest break"):
        resume_checkpoint(root, initial_path, 1)

    resumed_environments: list[TaskLocalEnv] = []

    def receive_notification(*args, **kwargs):
        meta, state, saved = read_snapshot(*args, **kwargs)
        assert isinstance(saved, Continuation)
        resumed_environments.append(saved.env)
        state["smp_started"].signaled = True
        return meta, state, saved

    monkeypatch.setattr("framework.interpreter.read_snapshot", receive_notification)
    resume_checkpoint(root, path, 1)
    assert root.worker.history == [1]
    assert root.worker.state.name == "RUNNING"
    (resumed_env,) = resumed_environments
    assert not resumed_env.signal_queues and resumed_env.depth == 0
    assert all(stack == [None] for stack in resumed_env.cv.stacks.values())


def test_plain_break_defers_guarded_drive_output_until_resume(root, tmp_path):
    cpu_model(
        root,
        tmp_path,
        """
        Def start():
            Actions:
                with GuardSmpAtomic(env.cv, self):
                    drive("self", "_pause")
        Def _pause():
            Actions:
                break(CpuFlowState.BUSYCHECK)
                self.result = env.cv.remote_tasks
        """,
    )
    path = start_with_break(root)
    _, _, saved = read_snapshot(root, path, model_version=1)
    assert isinstance(saved, Continuation)
    assert saved.history.pending == {"example_0.0"}
    assert saved.env.cv.remote_tasks == -1
    assert snapshot_paths.branch_outputs("example", 0)[-1] == path

    resume_checkpoint(root, path, 1)
    restored, meta = output(2)
    assert restored.worker.result == -1
    assert meta.get("input_point") == "example_0.0"


def test_resume_preserves_pending_emits_and_primary_return_value(root, tmp_path):
    cpu_model(
        root,
        tmp_path,
        """
        Def start():
            Actions:
                self.result = drive("self", "_dispatch")
                self.history.append(4)
            Ensures:
                self.state is CpuFlowState.BUSYCHECK
        Def _dispatch() -> result:
            Actions:
                result = drive("self", "_first")
        Def _first() -> result:
            Actions:
                emit("other", "pause")
                emit("self", "record")
                result = 7
        Def pause():
            Actions:
                self.history.append(1)
                break(CpuFlowState.BUSYCHECK)
                self.history.append(2)
        Def record():
            Actions:
                self.history.append(3)
        """,
    )
    path = start_with_break(root)
    _, _, saved = read_snapshot(root, path, model_version=1)
    assert isinstance(saved, Continuation)
    assert saved.history.pending == {"example_0.0"}
    assert [drive.caller_point for drive in saved.drives] == [None, "example_0.0", None]
    assert root.worker.result == 0
    assert root.worker.history == []
    assert root.other.history == [1]
    resume_checkpoint(root, path, 1)
    assert root.worker.result == 7
    assert root.worker.history == [3, 4]
    assert root.other.history == [1, 2]
    assert root.other.state.name == "RUNNING"


def test_labeled_break_fork_reuses_original_arguments_and_return_position(
    root, tmp_path
):
    cpu_model(
        root,
        tmp_path,
        """
        Def start():
            Actions:
                self.result = drive("self", "wait_for", value=self.result)
                self.history.append(self.result)
        Def wait_for(value) -> result:
            Actions:
                with GuardSpinLock(env.cv, self):
                    result = drive("self", "_read", value=value)
            Ensures:
                result == value + 1
        Def _read(value) -> result:
            Requires:
                not gv.smp_started.signaled
            Actions:
                break(CpuFlowState.BUSYCHECK, "self", "wait_for")
            Requires:
                gv.smp_started.signaled
            Actions:
                result = value + 1
        """,
    )
    candidate = snapshot_paths.snapshot_path("candidate", 0)
    candidate.parent.mkdir(parents=True)
    root.worker.result = 99
    root.smp_started.signaled = True
    save_snapshot(root, candidate, checkpoint=candidate.stem, model_version=1)
    root.worker.result = 7
    root.smp_started.signaled = False

    anchors: dict[str, CapturedInput] = {}
    with (
        pytest.raises(FlowPaused) as pause,
        FlowRun(
            root,
            TaskLocalEnv("example"),
            1,
            capture_input=anchors.__setitem__,
            capture_all_inputs=True,
        ) as run,
    ):
        System().drive(run.env, "worker", "start")
    assert root.worker.result == 7 and root.worker.history == []
    point = pause.value.point
    fork_input(root, point, anchors[point], candidate, 1)

    assert root.worker.state.name == "BUSYCHECK"
    assert root.worker.result == 8 and root.worker.history == [8]
    assert output(1, branch=1)[1].get("input_point") == "example_0.0"


def test_labeled_break_rejects_a_restart_outside_the_stack(root, tmp_path):
    cpu_model(
        root,
        tmp_path,
        """
        Def start():
            Actions:
                break(CpuFlowState.BUSYCHECK, "self", "unused")
        Def unused():
            Actions:
                pass
        """,
    )
    with (
        pytest.raises(SpecError, match="self.unused must be an active call"),
        FlowRun(root, TaskLocalEnv("example"), 1) as run,
    ):
        System().drive(run.env, "worker", "start")
    assert root.worker.state.name == "RUNNING"
    assert run.output_position == 1


def test_resume_validates_file_model_and_specs_before_installing_gv(
    root, tmp_path, monkeypatch
):
    spec = cpu_model(
        root,
        tmp_path,
        """
        Def start():
            Actions:
                break(CpuFlowState.BUSYCHECK)
        """,
    )
    path = start_with_break(root)
    content = path.read_bytes()
    previous = root.worker
    with pytest.raises(SnapshotCompatibilityError, match="model_version"):
        resume_checkpoint(root, path, 2)
    path.write_bytes(content[:-1])
    with pytest.raises(SnapshotFormatError, match="checksum"):
        resume_checkpoint(root, path, 1)
    path.write_bytes(content)

    def read_old_checkpoint(*args, **kwargs):
        meta, state, saved = read_snapshot(*args, **kwargs)
        assert isinstance(saved, Continuation)
        del saved.layout
        return meta, state, saved

    with monkeypatch.context() as patch:
        patch.setattr("framework.interpreter.read_snapshot", read_old_checkpoint)
        with pytest.raises(SnapshotCompatibilityError, match="instruction layout"):
            resume_checkpoint(root, path, 1)
    assert root.worker is previous

    spec.write_text(spec.read_text() + "\n# changed\n")
    with pytest.raises(SnapshotCompatibilityError, match="spec changed"):
        resume_checkpoint(root, path, 1)
    assert root.worker is previous


def test_break_requires_a_cpu_flow(root, tmp_path):
    model(
        root,
        tmp_path,
        """
        Def start():
            Actions:
                break("stopped")
        """,
    )
    with (
        pytest.raises(SpecError, match="requires a CpuFlow"),
        FlowRun(root, TaskLocalEnv(name="example"), 1) as run,
    ):
        System().drive(run.env, "worker", "start")
    assert run.output_position == 1


@pytest.mark.parametrize("buffered", [False, True])
def test_fork_both_entry_points_with_independent_candidate_gvs(
    root, tmp_path, monkeypatch, buffered
):
    cpu_model(
        root,
        tmp_path,
        """
        Def start():
            Requires:
                self.state is CpuFlowState.RUNNING
            Actions:
                drive("smp_started", "wait")
                self.state = CpuFlowState.RUNNING
                self.history.append(1)
            Ensures:
                self.state is CpuFlowState.BUSYCHECK
                self.history == [1]
        """,
    )
    candidates = [snapshot_paths.snapshot_path("candidate", i) for i in range(2)]
    candidates[0].parent.mkdir(parents=True)
    for i, path in enumerate(candidates):
        root.smp_started.signaled = bool(i)
        save_snapshot(root, path, checkpoint=path.stem, model_version=1)
    root.smp_started.signaled = False
    original = [path.read_bytes() for path in candidates]

    record_path = snapshot_paths.input_records_path("example")
    writes = []
    replace = flow_run.os.replace

    def track_record_publish(source, destination):
        if destination == record_path:
            writes.append(destination)
        return replace(source, destination)

    monkeypatch.setattr(flow_run.os, "replace", track_record_publish)
    anchors: dict[str, CapturedInput] = {}
    with (
        buffer_input_records() if buffered else nullcontext(),
        deduplicate_outputs(pack_breaks=True) if buffered else nullcontext(),
    ):
        with (
            pytest.raises(FlowPaused) as initial,
            FlowRun(
                root,
                TaskLocalEnv(name="example"),
                1,
                capture_input=anchors.__setitem__,
                capture_all_inputs=True,
            ) as run,
        ):
            System().drive(run.env, "worker", "start")
        with pytest.raises(FlowPaused) as retry:
            resume_checkpoint(root, initial.value.checkpoint, 1)
        assert list(anchors) == ["example_0.0", "example_0.1", "example_0.2"]
        entries = ["example_0.0", initial.value.point]
        baseline = list(run.records) if buffered else points()
        for base in entries:
            execution = anchors[base]
            with pytest.raises(FlowPaused):
                fork_input(root, base, execution, candidates[0], 1)
            assert root.worker.state.name == "BUSYCHECK"
            assert not root.worker.history
            fork_input(root, base, execution, candidates[1], 1)
            assert root.worker.state.name == "RUNNING"
            assert root.worker.history == [1]
        if buffered:
            assert not record_path.exists()
            assert not writes

    if buffered:
        assert writes == [record_path]
    records = points()
    for i, base in enumerate(entries):
        for offset, path in enumerate(candidates):
            point = next(
                record
                for record in records
                if record["name"] == f"example_{i * 2 + offset + 1}.0"
            )
            assert point["base"] == base
            assert point["input_snapshot"] == f"candidate/outputs/{path.name}"
            assert point["operation"]["kind"] == ("drive" if i == 0 else "with")
            assert point["location"]["method"] == ("start" if i == 0 else "wait")
    assert records[: len(baseline)] == baseline
    assert [path.read_bytes() for path in candidates] == original
    # Forks leave the original branch's last checkpoint available to resume.
    with pytest.raises(FlowPaused):
        resume_checkpoint(root, retry.value.checkpoint, 1)


def test_fork_reuses_prepared_signal_and_selected_contract(root, tmp_path):
    model(
        root,
        tmp_path,
        """
        Prop value: int = 1
        Prop result: int = 0
        Def start():
            Requires:
                self.value == 1
            Actions:
                self.result = drive("self", "read", value=self.value)
        Def read(value) -> result:
            Actions:
                result = value
        """,
    )
    candidate = snapshot_paths.snapshot_path("candidate", 0)
    candidate.parent.mkdir(parents=True)
    root.worker.value = 2
    save_snapshot(root, candidate, checkpoint=candidate.stem, model_version=1)
    root.worker.value = 1

    anchors: dict[str, CapturedInput] = {}
    with FlowRun(
        root, TaskLocalEnv(name="example"), 1, capture_input=anchors.__setitem__
    ) as run:
        System().drive(run.env, "worker", "start")
    fork_input(root, "example_0.0", anchors["example_0.0"], candidate, 1)
    assert root.worker.value == 2
    assert root.worker.result == 1

    before = points()
    root.worker = None
    incompatible = snapshot_paths.snapshot_path("candidate", 1)
    save_snapshot(root, incompatible, checkpoint=incompatible.stem, model_version=1)
    with pytest.raises(SnapshotCompatibilityError, match="incompatible type.*worker"):
        fork_input(root, "example_0.0", anchors["example_0.0"], incompatible, 1)
    assert points() == before


def test_guarded_drive_forks_before_guards_and_saves_after_exit(
    root, tmp_path, monkeypatch
):
    model(
        root,
        tmp_path,
        """
        Prop value: int = 3
        Prop observed: int = 0
        Def start():
            Actions:
                with GuardSmpAtomic(env.cv, self), GuardPreemption(env.cv):
                    result = drive("self", "_read", value=self.value, level=env.cv.local_tasks)
                self.observed += result
        Def _read(value, level) -> result:
            Requires:
                level == -2 and env.cv.stacks["remote_tasks"][-1] is self
            Actions:
                self.observed = value
                result = value + 1
        """,
    )
    candidate = snapshot_paths.snapshot_path("candidate", 0)
    candidate.parent.mkdir(parents=True)
    root.worker.value = 9
    save_snapshot(root, candidate, checkpoint=candidate.stem, model_version=1)
    root.worker.value = 3
    env = TaskLocalEnv("example")
    anchors: dict[str, CapturedInput] = {}
    events: list[str] = []
    original_save = flow_run.save_output_snapshot

    def capture(point, frozen):
        assert all(stack == [None] for stack in env.cv.stacks.values())
        events.append("capture")
        anchors[point] = frozen

    def observe_save(root, path, **kwargs):
        if path == snapshot_paths.snapshot_path("example", 1):
            assert all(stack == [None] for stack in env.cv.stacks.values())
            events.append("save")
        return original_save(root, path, **kwargs)

    monkeypatch.setattr(flow_run, "save_output_snapshot", observe_save)
    with FlowRun(root, env, 1, capture_input=capture):
        System().drive(env, "worker", "start")
    assert events == ["capture", "save"]
    assert root.worker.observed == 7
    assert [p["operation"]["kind"] for p in points()] == ["with", "bare"]
    assert output(1)[0].worker.observed == 3

    fork_input(root, "example_0.0", anchors["example_0.0"], candidate, 1)
    assert root.worker.observed == 19
    assert output(0, branch=1)[0].worker.observed == 9


def test_fork_keeps_prepared_argument_from_old_gv_container(root, tmp_path):
    model(
        root,
        tmp_path,
        """
        Prop table: list[System]
        Prop result: int = 0
        Def start():
            Actions:
                self.result = drive("self", "read", item=self.table[0])
        Def read(item) -> result:
            Actions:
                result = item.releases
        """,
    )
    candidate = snapshot_paths.snapshot_path("candidate", 0)
    candidate.parent.mkdir(parents=True)
    root.worker.table = [NativeTarget(releases=20)]
    save_snapshot(root, candidate, checkpoint=candidate.stem, model_version=1)
    root.worker.table = [NativeTarget(releases=7)]

    inputs: dict[str, CapturedInput] = {}
    with FlowRun(
        root, TaskLocalEnv(name="example"), 1, capture_input=inputs.__setitem__
    ) as run:
        System().drive(run.env, "worker", "start")
    fork_input(root, "example_0.0", inputs["example_0.0"], candidate, 1)

    assert root.worker.result == 7
    assert root.worker.table[0].releases == 20


def test_derivation_combines_actual_outputs_of_three_flows(root, tmp_path):
    model(
        root,
        tmp_path,
        """
        Prop seen: list[int]
        Def start(delta):
            Actions:
                self.seen.append(delta)
        """,
    )
    worker = type(root.worker)

    def reset():
        root.worker = worker()

    deltas = {"alpha": 2, "beta": 3, "gamma": 5}
    origins = [
        DerivationOrigin(TaskLocalEnv(name), delta) for name, delta in deltas.items()
    ]
    derive_flows(origins, root=root, reset=reset, model_version=1)

    all_orders = set()
    for target, delta in deltas.items():
        sources = deltas.keys() - {target}
        assert [output(i, target)[0].worker.seen for i in range(2)] == [[], [delta]]
        entries = [entry for entry in points(target) if entry["base"] is not None]
        assert {
            entry["input_snapshot"]
            for entry in entries
            if "_0." in entry["input_snapshot"]
        } == {f"{source}/outputs/{source}_0.1.snap" for source in sources}
        for entry in entries:
            branch = int(entry["name"].rsplit("_", 1)[1].split(".")[0])
            all_orders.add(tuple(output(0, target, branch)[0].worker.seen))
    assert all_orders >= {
        (first, second, third)
        for first in deltas.values()
        for second in deltas.values()
        for third in deltas.values()
        if len({first, second, third}) == 3
    }


def test_busy_wait_dependencies_and_feedback_to_publisher(root, tmp_path):
    cpu_model(
        root,
        tmp_path,
        """
        Def publish():
            Actions:
                drive("other_wait", "notify")
                drive("smp_started", "notify")
                self.result = len(gv.other.history)
        Def observe():
            Actions:
                self.history.append(7)
                drive("smp_started", "wait")
                self.result = 9
        """,
    )
    cpu, wait = type(root.worker), type(root.smp_started)

    def reset():
        root.worker, root.other = cpu(), cpu()
        root.smp_started, root.other_wait = wait(), wait()

    derive_flows(
        [
            ActionOrigin(TaskLocalEnv("bp"), "worker", "publish"),
            ActionOrigin(TaskLocalEnv("ap"), "other", "observe"),
        ],
        root=root,
        reset=reset,
        model_version=1,
    )

    records = {
        name: {point["name"]: point for point in points(name)} for name in ("bp", "ap")
    }
    prep_path = snapshot_paths.snapshot_path("ap", 1)
    prep_meta, prep_gv, _ = read_snapshot(root, prep_path, model_version=1)
    assert prep_gv["other"].history == [7]
    assert "waits_for" not in prep_meta
    paused, _, _ = read_snapshot(
        root, snapshot_paths.snapshot_path("ap", 2), model_version=1
    )
    assert "waits_for" not in paused
    before_notify = [
        entry
        for entry in records["bp"].values()
        if entry["base"] and Path(entry["input_snapshot"]).name == prep_path.name
    ]
    assert before_notify
    assert all(
        "smp_started" not in records["bp"][entry["base"]].get("notified", [])
        for entry in before_notify
    )
    assert any(
        record.get("notified") == ["other_wait"] for record in records["bp"].values()
    )

    feedback = []
    returned = []
    after_other_notify = []
    for entry in records["bp"].values():
        if entry["base"] is None:
            continue
        path = tmp_path / entry["input_snapshot"]
        metadata, state, _ = read_snapshot(root, path, model_version=1)
        base = records["bp"][entry["base"]]
        waits_for = metadata.get("waits_for", [])
        if waits_for:
            assert waits_for == ["smp_started"]
            assert "smp_started" in base["notified"]
        elif (
            base.get("notified") == ["other_wait"]
            and state["other_wait"].signaled
            and state["other"].history == [7]
        ):
            after_other_notify.append(entry)
        if (
            base["operation"]["text"] == "self.result = len(gv.other.history)"
            and state["other"].history == [7]
            and state["smp_started"].signaled
        ):
            (returned if waits_for else feedback).append(entry)
    assert feedback
    assert returned
    assert after_other_notify
    branch = int(returned[0]["name"].rsplit("_", 1)[1].split(".")[0])
    gv, _ = output(0, "bp", branch)
    assert gv.worker.result == 1 and gv.other.history == [7]
    assert gv.smp_started.signaled and gv.other_wait.signaled


@pytest.mark.parametrize("phase", ["requires", "ensures"])
def test_derivation_aborts_at_first_contract_failure(root, tmp_path, phase):
    condition = "delta == 1 or self.value == 0"
    contract = f"    {phase.title()}:\n        {condition}\n"
    actions = "    Actions:\n        pass\n"
    model(
        root,
        tmp_path,
        "Prop value: int = 0\n"
        "Def start(delta):\n"
        "    Actions:\n"
        '        drive("self", "check", delta=delta)\n'
        "        self.value = delta\n"
        "Def check(delta):\n"
        + (contract + actions if phase == "requires" else actions + contract),
    )
    worker = type(root.worker)

    def reset():
        root.worker = worker()

    origins = [
        DerivationOrigin(TaskLocalEnv("source"), 1),
        DerivationOrigin(TaskLocalEnv("target"), 2),
    ]
    with pytest.raises(ContractViolation) as failure:
        derive_flows(origins, root=root, reset=reset, model_version=1)

    assert failure.value.phase == phase
    entries = [point for point in points("target") if point["base"] is not None]
    assert [point["base"] for point in entries] == ["target_0.0"] * 2
    assert entries[-1]["input_snapshot"] == "source/outputs/source_0.2.snap"
    assert snapshot_paths.branch_outputs("target", 2) == []
    assert all(point["base"] is None for point in points("source"))


def test_derivation_resumes_each_break_site_once_per_line(root, tmp_path):
    cpu_model(
        root,
        tmp_path,
        """
        Def start(delta):
            Actions:
                break(CpuFlowState.BUSYCHECK)
                self.result = delta
                drive("smp_started", "wait")
        """,
    )
    worker = type(root.worker)

    def reset():
        root.worker = worker()

    derive_flows(
        [DerivationOrigin(TaskLocalEnv("example"), 9)],
        root=root,
        reset=reset,
        model_version=1,
    )

    records = points()
    breaks = [record for record in records if record["operation"]["kind"] == "break"]
    assert len(breaks) == 3
    first, wait, repeated = breaks
    assert first["location"] != wait["location"] == repeated["location"]
    assert root.worker.result == 9


def test_buffered_records_publish_on_contract_failure(root, tmp_path):
    model(
        root,
        tmp_path,
        """
        Prop value: int = 0
        Def good():
            Actions:
                self.value += 1
        Def fail():
            Requires:
                False
            Actions:
                pass
        Def start():
            Actions:
                drive("self", "fail")
        """,
    )
    with pytest.raises(ContractViolation), buffer_input_records():
        with FlowRun(root, TaskLocalEnv(name="good"), 1) as run:
            System().drive(run.env, "worker", "good")
        assert not snapshot_paths.input_records_path("good").exists()
        with FlowRun(root, TaskLocalEnv(name="example"), 1) as run:
            System().drive(run.env, "worker", "start")

    assert points("good")[0]["operation"]["text"] == "self.value += 1"
    failed = points()
    assert len(failed) == 1 and failed[0]["operation"]["kind"] == "drive"
    assert snapshot_paths.branch_outputs("example", 0) == [
        snapshot_paths.snapshot_path("example", 0)
    ]
