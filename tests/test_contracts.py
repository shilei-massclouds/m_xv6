"""Explicit contracts fail at the caller and preserve snapshot independence."""

import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from framework import snapshot_paths
from framework.contracts import Condition, ContractViolation, ensures, requires
from framework.engine import System, TaskLocalEnv
from framework.sync_primitives import LocalMultiTasks
from global_vars import GlobalVars, gv
from kernel.proc import ProcState, SchedState, Scheduler
from origins.ap_init_origin import ApInitOrigin
from origins.bp_init_origin import BpInitOrigin

REPO_ROOT = Path(__file__).resolve().parents[1]
type Check = Callable[[Condition], None]


@pytest.fixture
def model() -> Iterator[GlobalVars]:
    previous_state = gv.__dict__
    gv.__dict__ = vars(GlobalVars())
    try:
        yield gv
    finally:
        gv.__dict__ = previous_state


def _true() -> bool:
    return True


def _false() -> bool:
    return False


@pytest.mark.parametrize("check", [requires, ensures])
@pytest.mark.parametrize("condition", [True, lambda: True, _true])
def test_true_conditions_succeed(check: Check, condition: Condition) -> None:
    assert check(condition) is None


@pytest.mark.parametrize("check", [requires, ensures])
@pytest.mark.parametrize("condition", [False, lambda: False, _false])
def test_false_conditions_report_the_explicit_call_site(
    check: Check, condition: Condition
) -> None:
    line = sys._getframe().f_lineno + 2
    with pytest.raises(ContractViolation) as caught:
        check(condition)

    violation = caught.value
    phase = "requires" if check is requires else "ensures"
    assert violation.phase == phase
    assert violation.filename == __file__
    assert violation.lineno == line
    assert (
        violation.function
        == test_false_conditions_report_the_explicit_call_site.__qualname__
    )
    assert f"{phase} failed at {__file__}:{line}" in str(violation)
    assert violation.function in str(violation)


@pytest.mark.parametrize("check", [requires, ensures])
@pytest.mark.parametrize("result", [False, True])
def test_predicates_are_called_once(check: Check, result: bool) -> None:
    calls = 0

    def predicate() -> bool:
        nonlocal calls
        calls += 1
        return result

    if result:
        check(predicate)
    else:
        with pytest.raises(ContractViolation):
            check(predicate)

    assert calls == 1


@pytest.mark.parametrize("check", [requires, ensures])
@pytest.mark.parametrize("condition", [None, 0, 1, "False", lambda: None, lambda: 1])
def test_non_boolean_conditions_are_rejected(check: Check, condition: Any) -> None:
    with pytest.raises(TypeError, match="expects a bool or a callable returning bool"):
        check(condition)


@pytest.mark.parametrize("check", [requires, ensures])
def test_predicate_exceptions_propagate(check: Check) -> None:
    error = RuntimeError("predicate evaluation failed")

    def predicate() -> bool:
        raise error

    with pytest.raises(RuntimeError) as caught:
        check(predicate)

    assert caught.value is error


def test_methods_can_check_parameters_and_locals_through_closures() -> None:
    class Counter:
        def __init__(self):
            self.value = 1

        def is_positive(self) -> bool:
            return self.value > 0

        def increase(self, amount: int) -> None:
            requires(self.is_positive)
            requires(lambda: amount > 0)
            previous = self.value
            self.value += amount

            def increased() -> bool:
                return self.value > previous

            ensures(increased)

    counter = Counter()
    counter.increase(3)
    assert counter.value == 4


def test_method_diagnostics_use_the_qualified_name() -> None:
    class Example:
        def run(self) -> None:
            ensures(False)

    with pytest.raises(ContractViolation) as caught:
        Example().run()

    assert caught.value.function == Example.run.__qualname__


def test_the_first_failure_stops_later_checks_and_operations() -> None:
    calls: list[str] = []

    def condition(name: str) -> bool:
        calls.append(name)
        return False

    def run() -> None:
        requires(lambda: condition("first"))
        requires(lambda: condition("second"))
        calls.append("body")

    with pytest.raises(ContractViolation):
        run()

    assert calls == ["first"]


@pytest.mark.parametrize("phase", ["requires", "ensures"])
@pytest.mark.parametrize("optimization", [(), ("-O",)])
def test_unhandled_failures_terminate_and_report_location_under_optimization(
    phase: str, optimization: tuple[str, ...]
) -> None:
    script = (
        f"from framework.contracts import {phase}\n"
        "def rejected():\n"
        f"    {phase}(lambda: False)\n"
        "    print('unreachable')\n"
        "rejected()\n"
    )
    result = subprocess.run(
        [sys.executable, *optimization, "-c", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert "ContractViolation" in result.stderr
    assert f"{phase} failed at <string>:3 in rejected" in result.stderr


@pytest.mark.parametrize("state", [SchedState.SWITCHED, SchedState.WFI])
def test_bp_entry_failure_prevents_boot_operations(
    model: GlobalVars, monkeypatch: pytest.MonkeyPatch, state: SchedState
) -> None:
    model.bp_scheduler.state = state
    env = TaskLocalEnv()

    def no_operations(*args: Any, **kwargs: Any) -> None:
        pytest.fail("boot operations must not run after an entry failure")

    monkeypatch.setattr(System, "drive", no_operations)
    current_state = vars(model)

    with pytest.raises(ContractViolation) as caught:
        model.bp_init_flow.start(env)

    assert caught.value.phase == "requires"
    assert caught.value.function == "BpInitFlow.start"
    assert vars(model) is current_state
    assert model.bp_scheduler.state is state
    assert model.initproc.pid == 0
    assert list(snapshot_paths.SNAPSHOT_DIR.iterdir()) == []


@pytest.mark.parametrize("state", [SchedState.SWITCHED, SchedState.WFI])
def test_ap_entry_failure_prevents_environment_changes(
    model: GlobalVars, monkeypatch: pytest.MonkeyPatch, state: SchedState
) -> None:
    model.initproc.state = ProcState.RUNNABLE
    model.bp_scheduler.state = state
    env = TaskLocalEnv()

    def no_operations(*args: Any, **kwargs: Any) -> None:
        pytest.fail("AP synchronization must not run after an entry failure")

    monkeypatch.setattr(LocalMultiTasks, "enable", no_operations)

    with pytest.raises(ContractViolation) as caught:
        model.ap_init_flow.start(env)

    assert caught.value.phase == "requires"
    assert caught.value.function == "ApInitFlow.start"
    assert model.bp_scheduler.state is state
    assert model.ap_scheduler.state is SchedState.RUNNING
    assert env.cv.local_tasks == 0
    assert list(snapshot_paths.SNAPSHOT_DIR.iterdir()) == []


def test_bp_exit_failure_keeps_the_recorded_snapshot(
    model: GlobalVars, monkeypatch: pytest.MonkeyPatch
) -> None:
    def schedule(self: Scheduler, env: TaskLocalEnv) -> None:
        assert self is model.bp_scheduler
        # Leave the scheduler RUNNING to fail the exit contract, and change
        # process state to check that the earlier snapshot stays independent.
        model.initproc.state = ProcState.RUNNING

    monkeypatch.setattr(Scheduler, "schedule", schedule)
    origin = BpInitOrigin()

    with pytest.raises(ContractViolation) as caught:
        origin.start()

    assert caught.value.phase == "ensures"
    assert caught.value.function == "BpInitFlow.start"
    assert origin.env.depth == 0
    assert origin.env.signal_queues == []
    restored = GlobalVars()
    restored.load_snapshot(
        snapshot_paths.BP_INITIALIZED_SNAPSHOT_PATH,
        expected_checkpoint="bp_initialized",
    )
    assert restored.bp_init_flow.hartid == 0
    assert restored.initproc.state is ProcState.RUNNABLE
    assert restored.childproc.state is ProcState.UNUSED
    assert restored.bp_scheduler.state is SchedState.RUNNING
    assert restored.ap_scheduler.state is SchedState.RUNNING
    assert model.bp_scheduler.state is SchedState.RUNNING
    assert model.initproc.state is ProcState.RUNNING


def test_ap_exit_failure_does_not_rewrite_the_loaded_snapshot(
    model: GlobalVars, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = GlobalVars()
    source.initproc.state = ProcState.RUNNABLE
    path = snapshot_paths.BP_INITIALIZED_SNAPSHOT_PATH
    source.save_snapshot(path, checkpoint="bp_initialized")
    saved = path.read_bytes()

    def schedule(self: Scheduler, env: TaskLocalEnv) -> None:
        assert self is model.ap_scheduler
        # Keep the AP scheduler RUNNING so its exit contract rejects the call.
        model.initproc.state = ProcState.RUNNING

    monkeypatch.setattr(Scheduler, "schedule", schedule)
    origin = ApInitOrigin()

    with pytest.raises(ContractViolation) as caught:
        origin.start()

    assert caught.value.phase == "ensures"
    assert caught.value.function == "ApInitFlow.start"
    assert origin.env.depth == 0
    assert origin.env.signal_queues == []
    assert model.ap_scheduler.state is SchedState.RUNNING
    assert model.initproc.state is ProcState.RUNNING
    assert path.read_bytes() == saved
