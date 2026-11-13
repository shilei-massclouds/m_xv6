"""Self targets retain the emitting instance across queued and nested calls."""

from collections import deque
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from framework.contention import ContentionVector
from framework.engine import Signal, System, TaskLocalEnv
from global_vars import GlobalVars, gv
from kernel.proc import NextRunnable, Scheduler


@pytest.fixture
def model() -> Iterator[Any]:
    previous_state = gv.__dict__
    gv.__dict__ = vars(GlobalVars())
    try:
        yield gv
    finally:
        gv.__dict__ = previous_state


class Probe(System):
    def __init__(self):
        self.events: list[tuple[str, str]] = []
        self.values: list[object] = []

    def acquire(self, env: TaskLocalEnv, action: str):
        self.events.append(("acquire", action))
        super().acquire(env, action)

    def release(self, env: TaskLocalEnv, action: str):
        self.events.append(("release", action))
        super().release(env, action)

    def _record(self, env: TaskLocalEnv, *, value: object) -> object:
        self.events.append(("action", "_record"))
        self.values.append(value)
        return value

    def _fail(self, env: TaskLocalEnv) -> None:
        self.events.append(("action", "_fail"))
        raise RuntimeError("action failed")

    def _source_argument(self, env: TaskLocalEnv, source: object) -> object:
        return source


class NestedProbe(Probe):
    def run(self, env: TaskLocalEnv, value: object) -> object:
        return self.drive(env, "self", "_record", value=value)


class EmittingProbe(Probe):
    def run(self, env: TaskLocalEnv) -> str:
        self.emit(env, "self", "_record", value="first queued")
        self.emit(env, "self", "_record", value="second queued")
        return "initial result"


def test_drive_self_preserves_result_and_action_lifecycle() -> None:
    source = Probe()
    env = TaskLocalEnv()
    value = object()

    assert source.drive(env, "self", "_record", value=value) is value

    assert source.values == [value]
    assert source.events == [
        ("acquire", "_record"),
        ("action", "_record"),
        ("release", "_record"),
    ]
    assert env.depth == 0
    assert env.signal_queues == []


def test_nested_drive_binds_self_to_the_nested_caller(model: Any) -> None:
    caller = Probe()
    receiver = NestedProbe()
    model.receiver = receiver
    env = TaskLocalEnv()

    result = caller.drive(env, "receiver", "run", value="nested")

    assert result == "nested"
    assert receiver.values == ["nested"]
    assert caller.values == []
    assert env.depth == 0
    assert env.signal_queues == []


def test_emit_binds_self_to_the_emitter_and_preserves_queue_order(model: Any) -> None:
    caller = Probe()
    receiver = EmittingProbe()
    model.receiver = receiver
    env = TaskLocalEnv()

    result = caller.drive(env, "receiver", "run")

    assert result == "initial result"
    assert receiver.values == ["first queued", "second queued"]
    assert caller.values == []
    assert receiver.events[:3] == [
        ("acquire", "run"),
        ("release", "run"),
        ("acquire", "_record"),
    ]
    assert env.depth == 0
    assert env.signal_queues == []


def test_named_and_dotted_targets_still_resolve_through_globals(model: Any) -> None:
    caller = Probe()
    receiver = Probe()
    model.holder = SimpleNamespace(receiver=receiver)

    result = caller.drive(TaskLocalEnv(), "holder.receiver", "_record", value="dotted")

    assert result == "dotted"
    assert receiver.values == ["dotted"]
    assert caller.values == []


def test_self_signal_retains_its_source_when_global_references_change(
    model: Any,
) -> None:
    source = Probe()
    replacement = Probe()
    model.receiver = source
    signal = Signal(
        source=source,
        target="self",
        action="_record",
        args={"value": "captured source"},
        env=TaskLocalEnv(),
        queue=deque(),
    )
    model.receiver = replacement

    assert signal.handle() == "captured source"

    assert signal.source is source
    assert signal.target == "self"
    assert source.values == ["captured source"]
    assert replacement.values == []


def test_self_target_checks_protection_on_the_correct_instance() -> None:
    source = Probe()
    other = Probe()
    env = TaskLocalEnv(
        cv=ContentionVector(local_irq=0, local_tasks=0, remote_irq=1, remote_tasks=1)
    )
    env.cv.protect(source, "remote_irq", "remote_tasks")

    assert source.drive(env, "self", "_record", value="protected") == "protected"
    with pytest.raises(AssertionError, match="Contention invariant violated"):
        other.drive(env, "self", "_record", value="unprotected")

    assert source.values == ["protected"]
    assert other.values == []
    assert other.events == [("acquire", "_record")]
    assert env.depth == 0
    assert env.signal_queues == []


def test_self_action_failure_still_releases_and_restores_queue_context() -> None:
    source = Probe()
    env = TaskLocalEnv()

    with pytest.raises(RuntimeError, match="action failed"):
        source.drive(env, "self", "_fail")

    assert source.events == [
        ("acquire", "_fail"),
        ("action", "_fail"),
        ("release", "_fail"),
    ]
    assert env.depth == 0
    assert env.signal_queues == []


def test_source_keyword_can_be_forwarded_to_the_action() -> None:
    source = Probe()
    argument = object()

    assert (
        source.drive(TaskLocalEnv(), "self", "_source_argument", source=argument)
        is argument
    )


@pytest.mark.parametrize("scheduler_name", ["bp_scheduler", "ap_scheduler"])
@pytest.mark.parametrize("next_runnable", [NextRunnable.INIT_PROC, NextRunnable.NONE])
def test_scheduler_self_calls_remain_on_the_invoked_instance(
    model: Any,
    monkeypatch: pytest.MonkeyPatch,
    scheduler_name: str,
    next_runnable: NextRunnable,
) -> None:
    calls: list[tuple[Scheduler, str]] = []

    def switch(self: Scheduler, env: TaskLocalEnv) -> None:
        calls.append((self, "switch"))

    def wfi(self: Scheduler, env: TaskLocalEnv) -> None:
        calls.append((self, "wfi"))

    monkeypatch.setattr(Scheduler, "_switch", switch)
    monkeypatch.setattr(Scheduler, "_wfi", wfi)
    model.proc_table.has_runnable = next_runnable is not NextRunnable.NONE
    scheduler = getattr(model, scheduler_name)
    env = TaskLocalEnv(
        cv=ContentionVector(local_irq=0, local_tasks=0, remote_irq=1, remote_tasks=1)
    )

    scheduler.schedule(env)

    assert len(calls) == 1
    assert calls[0][0] is scheduler
    assert calls[0][1] == ("wfi" if next_runnable is NextRunnable.NONE else "switch")
    assert env.depth == 0
    assert env.signal_queues == []
