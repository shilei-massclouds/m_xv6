"""Scheduling checkpoints feed independent InitProc initialization flows."""

import pickle
from collections.abc import Iterator
from typing import Any, Literal

import pytest

from flows.ap_init_flow import ApInitFlow
from flows.bp_init_flow import BpInitFlow
from flows.initproc_flow import InitProcFlow, Privilege
from framework import snapshot_paths
from framework.contention import DOMAINS, ContentionVector
from framework.engine import TaskLocalEnv
from framework.snapshot import SnapshotError, save_snapshot
from framework.sync_primitives import LocalIrq, LocalMultiTasks, Preemption, RemoteCpus
from global_vars import MODEL_VERSION, GlobalVars, gv
from kernel.proc import ProcState, SchedState, Scheduler
from origins.ap_init_origin import ApInitOrigin
from origins.bp_init_origin import BpInitOrigin
from origins.initproc_origin import InitProcOrigin


@pytest.fixture
def model() -> Iterator[Any]:
    previous_state = gv.__dict__
    gv.__dict__ = vars(GlobalVars())
    try:
        yield gv
    finally:
        gv.__dict__ = previous_state


def _levels(env: TaskLocalEnv) -> tuple[int, ...]:
    return tuple(getattr(env.cv, domain) for domain in DOMAINS)


@pytest.mark.parametrize("source", ["bp", "ap"])
@pytest.mark.parametrize("fail_schedule", [False, True])
def test_scheduler_saves_after_disabling_irq_before_scheduling(
    model: Any,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    fail_schedule: bool,
) -> None:
    path = getattr(snapshot_paths, f"{source.upper()}_BEFORE_SCHEDULE_SNAPSHOT_PATH")
    checkpoint = f"{source}_before_schedule"
    scheduler = getattr(model, f"{source}_scheduler")
    model.marker = [source]
    env = TaskLocalEnv(
        cv=ContentionVector(local_irq=0, local_tasks=0, remote_irq=1, remote_tasks=1)
    )
    events: list[str] = []
    original_enable = LocalIrq.enable
    original_disable = LocalIrq.disable
    original_save = GlobalVars.save_snapshot
    original_schedule = Scheduler._schedule

    def enable(self: LocalIrq, cv: ContentionVector) -> None:
        original_enable(self, cv)
        if "schedule" not in events:
            assert cv.local_irq == 1
            events.append("irq enabled")

    def disable(self: LocalIrq, cv: ContentionVector) -> None:
        original_disable(self, cv)
        if "schedule" not in events:
            assert cv.local_irq == 0
            events.append("irq disabled")

    def save(self: GlobalVars, destination: Any, *, checkpoint: str) -> Any:
        assert destination == path
        assert checkpoint == f"{source}_before_schedule"
        assert events == ["irq enabled", "irq disabled"]
        assert _levels(env) == (0, 0, 1, 1)
        assert scheduler.state is SchedState.RUNNING
        events.append("snapshot")
        return original_save(self, destination, checkpoint=checkpoint)

    def schedule(self: Scheduler, env: TaskLocalEnv) -> None:
        assert self is scheduler
        assert events == ["irq enabled", "irq disabled", "snapshot"]
        restored: Any = GlobalVars()
        restored.load_snapshot(path, expected_checkpoint=checkpoint)
        assert restored.marker == [source]
        assert getattr(restored, f"{source}_scheduler").state is SchedState.RUNNING
        events.append("schedule")
        model.marker.append("after snapshot")
        if fail_schedule:
            raise RuntimeError("scheduler stopped")
        original_schedule(self, env)

    monkeypatch.setattr(LocalIrq, "enable", enable)
    monkeypatch.setattr(LocalIrq, "disable", disable)
    monkeypatch.setattr(GlobalVars, "save_snapshot", save)
    monkeypatch.setattr(Scheduler, "_schedule", schedule)

    if fail_schedule:
        with pytest.raises(RuntimeError, match="scheduler stopped"):
            scheduler.schedule(env)
    else:
        scheduler.schedule(env)
        assert scheduler.state is SchedState.SWITCHED

    assert events == ["irq enabled", "irq disabled", "snapshot", "schedule"]
    restored: Any = GlobalVars()
    restored.load_snapshot(path, expected_checkpoint=checkpoint)
    assert restored.marker == [source]
    assert getattr(restored, f"{source}_scheduler").state is SchedState.RUNNING
    assert set(snapshot_paths.SNAPSHOT_DIR.iterdir()) == {path}
    assert _levels(env) == (0, 0, 1, 1)
    assert env.depth == 0
    assert env.signal_queues == []


@pytest.mark.parametrize("source", ["bp", "ap"])
@pytest.mark.parametrize("first", [True, False])
def test_initproc_restores_the_saved_flow_and_returns_to_user_mode(
    model: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    source: Literal["bp", "ap"],
    first: bool,
) -> None:
    model.saved_environment = TaskLocalEnv(depth=7)
    model.initproc_flow.marker = [source]
    model.first_proc.first = first
    bp = BpInitOrigin()
    bp.start()
    boot_envs = [bp.env]
    if source == "ap":
        ap = ApInitOrigin()
        ap.start()
        boot_envs.append(ap.env)
    before_files = {
        path: path.read_bytes() for path in snapshot_paths.SNAPSHOT_DIR.iterdir()
    }
    original_root = model
    stale_flow = model.initproc_flow
    stale_flow.marker = ["live"]
    stale_flow.priv = Privilege.USER
    model.first_proc.first = not first
    model.initproc.state = ProcState.RUNNING
    model.later = True
    origin = InitProcOrigin(source)
    other_origin = InitProcOrigin(source)
    original_start = InitProcFlow.start
    original_enable_local_tasks = LocalMultiTasks.enable
    original_enable_remote_cpus = RemoteCpus.enable
    calls: list[tuple[str, object]] = []

    def unexpected_operation(*args: Any, **kwargs: Any) -> None:
        pytest.fail(
            "InitProc must not rerun boot flows or change IRQ or preemption state"
        )

    def enable_local_tasks(self: LocalMultiTasks, cv: ContentionVector) -> None:
        assert cv is origin.env.cv
        assert _levels(origin.env) == (0, 0, 0, 0)
        calls.append(("enable local multitasking", cv))
        original_enable_local_tasks(self, cv)
        assert _levels(origin.env) == (0, 1, 0, 0)

    def enable_remote_cpus(self: RemoteCpus, cv: ContentionVector) -> None:
        assert cv is origin.env.cv
        assert _levels(origin.env) == (0, 1, 0, 0)
        calls.append(("enable remote CPUs", cv))
        original_enable_remote_cpus(self, cv)
        assert _levels(origin.env) == (0, 1, 1, 1)

    def start(self: InitProcFlow, env: TaskLocalEnv, **kwargs: Any) -> None:
        assert model is original_root
        assert self is model.initproc_flow
        assert self is not stale_flow
        assert model.initproc_flow.marker == [source]
        assert self.priv is Privilege.KERNEL
        assert model.first_proc.first is first
        assert model.initproc.state is ProcState.RUNNABLE
        assert model.childproc.state is ProcState.UNUSED
        assert model.bp_scheduler.state is SchedState.RUNNING
        assert model.ap_scheduler.state is SchedState.RUNNING
        assert not hasattr(model, "later")
        assert env is origin.env
        assert all(env is not boot_env for boot_env in boot_envs)
        assert env is not model.saved_environment
        assert env.cv is not model.saved_environment.cv
        assert model.saved_environment.depth == 7
        assert _levels(env) == (0, 1, 1, 1)
        state = vars(model)
        cv = env.cv
        saved_cv = pickle.dumps(vars(cv))

        assert original_start(self, env, **kwargs) is None

        assert vars(model) is state
        assert self.priv is Privilege.USER
        assert model.first_proc.first is False
        assert model.initproc.state is ProcState.RUNNABLE
        assert model.childproc.state is ProcState.UNUSED
        assert model.bp_scheduler.state is SchedState.RUNNING
        assert model.ap_scheduler.state is SchedState.RUNNING
        assert env.cv is cv
        assert pickle.dumps(vars(cv)) == saved_cv
        assert _levels(env) == (0, 1, 1, 1)
        calls.append(("start", self))

    monkeypatch.setattr(BpInitFlow, "start", unexpected_operation)
    monkeypatch.setattr(ApInitFlow, "start", unexpected_operation)
    monkeypatch.setattr(LocalIrq, "enable", unexpected_operation)
    monkeypatch.setattr(LocalIrq, "disable", unexpected_operation)
    monkeypatch.setattr(LocalMultiTasks, "enable", enable_local_tasks)
    monkeypatch.setattr(Preemption, "disable", unexpected_operation)
    monkeypatch.setattr(Preemption, "enable", unexpected_operation)
    monkeypatch.setattr(RemoteCpus, "enable", enable_remote_cpus)
    monkeypatch.setattr(InitProcFlow, "start", start)
    capsys.readouterr()

    origin.start()

    actions = [
        line.strip() for line in capsys.readouterr().out.splitlines() if " -> " in line
    ]
    expected_actions = [
        "start -> initproc_flow",
        "check -> first_proc",
        "_check -> self",
    ]
    if first:
        expected_actions.extend(
            ["_first_init -> self", "_init -> fs", "_kexec -> initproc"]
        )
    assert actions == [*expected_actions, "userret -> self"]
    assert calls == [
        ("enable local multitasking", origin.env.cv),
        ("enable remote CPUs", origin.env.cv),
        ("start", model.initproc_flow),
    ]
    assert origin.env is not other_origin.env
    assert origin.env.cv is not other_origin.env.cv
    assert origin.env.signal_queues is not other_origin.env.signal_queues
    assert _levels(other_origin.env) == (0, 0, 0, 0)
    assert _levels(origin.env) == (0, 1, 1, 1)
    assert origin.env.depth == 0
    assert origin.env.signal_queues == []
    assert {
        path: path.read_bytes() for path in snapshot_paths.SNAPSHOT_DIR.iterdir()
    } == before_files


@pytest.mark.parametrize("source", ["bp", "ap"])
@pytest.mark.parametrize(
    "failure", ["missing", "corrupt", "old_model", "wrong_checkpoint"]
)
def test_initproc_rejects_bad_sources_without_fallback_or_model_changes(
    model: Any,
    monkeypatch: pytest.MonkeyPatch,
    source: Literal["bp", "ap"],
    failure: str,
) -> None:
    path = getattr(snapshot_paths, f"{source.upper()}_BEFORE_SCHEDULE_SNAPSHOT_PATH")
    checkpoint = f"{source}_before_schedule"
    other = "ap" if source == "bp" else "bp"
    other_path = getattr(
        snapshot_paths, f"{other.upper()}_BEFORE_SCHEDULE_SNAPSHOT_PATH"
    )
    saved = GlobalVars()
    saved.save_snapshot(other_path, checkpoint=f"{other}_before_schedule")
    if failure == "corrupt":
        path.write_bytes(b"broken snapshot")
    elif failure == "old_model":
        old_state = vars(saved)
        del old_state["initproc_flow"]
        save_snapshot(saved, path, checkpoint=checkpoint, model_version=4)
        assert MODEL_VERSION == 5
    elif failure == "wrong_checkpoint":
        saved.save_snapshot(path, checkpoint=f"{other}_before_schedule")
    before_files = {
        path: path.read_bytes() for path in snapshot_paths.SNAPSHOT_DIR.iterdir()
    }
    model.marker = "live"
    current_state = vars(model)
    origin = InitProcOrigin(source)

    def no_flows(*args: Any, **kwargs: Any) -> None:
        pytest.fail("invalid InitProc snapshots must not run any Flow")

    monkeypatch.setattr(BpInitFlow, "start", no_flows)
    monkeypatch.setattr(ApInitFlow, "start", no_flows)
    monkeypatch.setattr(InitProcFlow, "start", no_flows)
    monkeypatch.setattr(LocalMultiTasks, "enable", no_flows)
    monkeypatch.setattr(RemoteCpus, "enable", no_flows)

    with pytest.raises(SnapshotError, match=path.name):
        origin.start()

    assert vars(model) is current_state
    assert model.marker == "live"
    assert _levels(origin.env) == (0, 0, 0, 0)
    assert origin.env.depth == 0
    assert origin.env.signal_queues == []
    assert {
        path: path.read_bytes() for path in snapshot_paths.SNAPSHOT_DIR.iterdir()
    } == before_files
