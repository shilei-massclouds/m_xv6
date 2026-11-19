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
from framework.flow_run import FlowRun, buffer_input_records, require_run
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
class Root:
    worker: Any = None
    native: NativeTarget = field(default_factory=NativeTarget)
    bad: Any = None
    other: Any = None
    smp_started: Any = None


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
    wait = Path(__file__).resolve().parents[1] / "kernel" / "SmpStarted.spec"
    registry = SpecRegistry([path, wait])
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
                with GuardSpinLock(env.cv, self):
                    assert self.value == 1
                    drive("self", "observe")
                    self.value = drive("self", "_value")
                pass
            Ensures:
                self.value == 7 and env.cv.local_irq == 0

        Def observe():
            Actions:
                self.value += 1

        Def _value() -> result:
            Actions:
                result = 7
    """,
    )
    env = TaskLocalEnv(name="example")
    captured: dict[str, bytes] = {}
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
            'drive("self", "observe")',
            "self.value += 1",
            'self.value = drive("self", "_value")',
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
    assert len(saved.history.pending) == 4
    assert saved.frames[-2].guards[0].target is state["smp_started"]
    assert saved.frames[-2].guards[0].cv is saved.env.cv
    assert all(
        stack[-1] is state["smp_started"] for stack in saved.env.cv.stacks.values()
    )
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
        assert len(saved.frames) == 3
        assert saved.env.depth == 3
        assert saved.history.output_position == 2 + 2 * attempt
        assert [r["operation"]["kind"] for r in points()[-2:]] == ["emit", "break"]
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


def test_resume_validates_file_model_and_specs_before_installing_gv(root, tmp_path):
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
    anchors: dict[str, bytes] = {}
    with (
        buffer_input_records() if buffered else nullcontext(),
        deduplicate_outputs(pack_breaks=True) if buffered else nullcontext(),
    ):
        with (
            pytest.raises(FlowPaused) as initial,
            FlowRun(
                root, TaskLocalEnv(name="example"), 1, capture_input=anchors.__setitem__
            ) as run,
        ):
            System().drive(run.env, "worker", "start")
        with pytest.raises(FlowPaused) as retry:
            resume_checkpoint(
                root, initial.value.checkpoint, 1, capture_input=anchors.__setitem__
            )
        assert list(anchors) == ["example_0.0", "example_0.3"]
        baseline = list(run.records) if buffered else points()
        for base, execution in anchors.items():
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
    for i, base in enumerate(anchors):
        for offset, path in enumerate(candidates):
            point = next(
                record
                for record in records
                if record["name"] == f"example_{i * 2 + offset + 1}.0"
            )
            assert point["base"] == base
            assert point["input_snapshot"] == f"candidate/outputs/{path.name}"
            assert point["operation"]["kind"] == ("drive" if i == 0 else "emit")
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

    anchors: dict[str, bytes] = {}
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

    inputs: dict[str, bytes] = {}
    with FlowRun(
        root, TaskLocalEnv(name="example"), 1, capture_input=inputs.__setitem__
    ) as run:
        System().drive(run.env, "worker", "start")
    fork_input(root, "example_0.0", inputs["example_0.0"], candidate, 1)

    assert root.worker.result == 7
    assert root.worker.table[0].releases == 20


def test_derivation_combines_every_i_point_in_order(root, tmp_path):
    model(
        root,
        tmp_path,
        """
        Prop value: int = 0
        Def start(delta):
            Actions:
                loaded = self.value
                self.value = loaded + delta
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

    def candidate(flow, index):
        return f"{flow}/outputs/{flow}_0.{index}.snap"

    for target, delta in deltas.items():
        sources = [name for name in deltas if name != target]
        baseline = {f"{target}_0.{index}" for index in range(2)}
        records = points(target)
        entries = [record for record in records if record["base"] is not None]
        second = [entry for entry in entries if entry["base"] in baseline]
        third = [entry for entry in entries if entry["base"] not in baseline]
        assert [(output(i, target)[0].worker.value) for i in range(3)] == [0, 0, delta]
        assert len(second) == 12
        assert {(entry["base"], entry["input_snapshot"]) for entry in second} == {
            (base, candidate(source, index))
            for base in baseline
            for source in sources
            for index in range(3)
        }
        expected = set()
        for entry in second:
            source = entry["input_snapshot"].split("/")[0]
            other = next(name for name in sources if name != source)
            branch = entry["name"].rsplit(".", 1)[0]
            expected.update(
                (record["name"], candidate(other, index))
                for record in records
                if record["name"].startswith(f"{branch}.")
                for index in range(3)
            )
        assert len(third) == 54
        assert {(entry["base"], entry["input_snapshot"]) for entry in third} == expected

        # Loading a third GV still preserves the value computed by the second line.
        source, other = sources
        seeded = next(
            entry
            for entry in second
            if entry["base"] == f"{target}_0.0"
            and entry["input_snapshot"] == candidate(source, 2)
        )
        late = seeded["name"].rsplit(".", 1)[0] + ".1"
        combined = next(
            entry
            for entry in third
            if entry["base"] == late and entry["input_snapshot"] == candidate(other, 0)
        )
        branch_number = int(combined["name"].rsplit("_", 1)[1].split(".")[0])
        assert (
            output(0, target, branch_number)[0].worker.value == delta + deltas[source]
        )


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
    assert [point["base"] for point in entries] == ["target_0.0"] * 3
    assert entries[-1]["input_snapshot"] == "source/outputs/source_0.2.snap"
    assert snapshot_paths.branch_outputs("target", 3) == []
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
