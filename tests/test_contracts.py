"""Branch contracts preserve entry selections and snapshot independence."""

import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from framework import snapshot_paths
from framework.contracts import ContractViolation, ensures, requires
from framework.engine import System, TaskLocalEnv
from framework.sync_primitives import LocalMultiTasks
from global_vars import GlobalVars, gv
from kernel.proc import NextRunnable, ProcState, SchedState, Scheduler
from origins.ap_init_origin import ApInitOrigin
from origins.bp_init_origin import BpInitOrigin

REPO_ROOT = Path(__file__).resolve().parents[1]
type Check = Callable[[Any], None]


@pytest.fixture
def model() -> Iterator[GlobalVars]:
    previous_state = gv.__dict__
    gv.__dict__ = vars(GlobalVars())
    try:
        yield gv
    finally:
        gv.__dict__ = previous_state


@pytest.mark.parametrize("branch_count", [1, 2, 7])
def test_entry_alternatives_select_the_corresponding_exit_row(
    branch_count: int,
) -> None:
    def run(selected: int) -> None:
        requires([index == selected for index in range(branch_count)])
        ensures([[False, index == selected, False] for index in range(branch_count)])

    for selected in range(branch_count):
        run(selected)


@pytest.mark.parametrize("conditions", [[], [False], [False, False, False]])
def test_unmatched_entries_report_the_explicit_call_site(
    conditions: list[bool],
) -> None:
    line = sys._getframe().f_lineno + 2
    with pytest.raises(ContractViolation) as caught:
        requires(conditions)

    violation = caught.value
    assert violation.phase == "requires"
    assert violation.filename == __file__
    assert violation.lineno == line
    assert (
        violation.function
        == test_unmatched_entries_report_the_explicit_call_site.__qualname__
    )
    assert violation.branch_index is None
    assert f"requires failed at {__file__}:{line}" in str(violation)
    assert violation.function in str(violation)


@pytest.mark.parametrize("selected", [0, 1, 2])
def test_an_unselected_exit_row_cannot_satisfy_the_contract(selected: int) -> None:
    requires([index == selected for index in range(3)])
    line = sys._getframe().f_lineno + 2
    with pytest.raises(ContractViolation) as caught:
        ensures([[index != selected] for index in range(3)])

    violation = caught.value
    assert violation.phase == "ensures"
    assert violation.filename == __file__
    assert violation.lineno == line
    assert violation.branch_index == selected
    assert f"entry branch {selected}" in str(violation)


def test_entry_selection_is_saved_before_arguments_and_state_change() -> None:
    entries = [False, True]
    requires(entries)
    entries[:] = [True, False]
    ensures([[False], [False, True]])


def test_unselected_empty_exit_rows_are_ignored() -> None:
    requires([False, True])
    ensures([[], [True]])


def test_an_empty_selected_exit_row_fails() -> None:
    requires([True])
    with pytest.raises(ContractViolation, match="entry branch 0"):
        ensures([[]])


def test_ensures_without_requires_uses_one_unconditional_entry() -> None:
    ensures([[False, True]])


@pytest.mark.parametrize(
    "conditions",
    [None, True, 1, "true", (True,), [1], [True, None], [True, lambda: True]],
)
def test_requires_rejects_non_lists_and_non_boolean_elements(conditions: Any) -> None:
    with pytest.raises(TypeError, match="expects a (list of bool|bool)"):
        requires(conditions)


@pytest.mark.parametrize(
    "branches",
    [None, True, ([],), [True], [[1]], [[True, None]], [[True], [lambda: True]]],
)
def test_ensures_validates_the_shape_and_all_rows(branches: Any) -> None:
    with pytest.raises(TypeError, match="expects a (list|bool)"):
        ensures(branches)


@pytest.mark.parametrize("rows", [[], [[True]], [[True], [True], [True]]])
def test_exit_row_count_must_match_entry_branch_count(rows: list[list[bool]]) -> None:
    requires([False, True])
    with pytest.raises(ValueError, match="row count.*expected 2"):
        ensures(rows)


def test_ensures_without_requires_requires_exactly_one_row() -> None:
    with pytest.raises(ValueError, match="row count.*expected 1"):
        ensures([[True], [True]])


def test_overlapping_entry_branches_are_rejected_for_the_current_call() -> None:
    with pytest.raises(ValueError, match="branches overlap.*0, 2"):
        requires([True, False, True])


@pytest.mark.parametrize(
    ("check", "argument"), [(requires, [True]), (ensures, [[True]])]
)
def test_each_contract_phase_can_only_be_called_once(
    check: Check, argument: Any
) -> None:
    check(argument)
    with pytest.raises(ValueError, match="at most once"):
        check(argument)


def test_requires_cannot_follow_ensures() -> None:
    ensures([[True]])
    with pytest.raises(ValueError, match="must precede"):
        requires([True])


def test_caught_entry_failures_cannot_be_followed_by_a_successful_exit() -> None:
    with pytest.raises(ContractViolation):
        requires([False])
    with pytest.raises(ValueError, match="successful requires"):
        ensures([[True]])


def test_methods_can_check_parameters_and_locals() -> None:
    class Counter:
        def __init__(self):
            self.value = 1

        def increase(self, amount: int) -> None:
            requires([self.value > 0 and amount > 0])
            previous = self.value
            self.value += amount
            ensures([[self.value > previous]])

    counter = Counter()
    counter.increase(3)
    assert counter.value == 4


def test_method_diagnostics_use_the_qualified_name() -> None:
    class Example:
        def run(self) -> None:
            ensures([[False]])

    with pytest.raises(ContractViolation) as caught:
        Example().run()

    assert caught.value.function == Example.run.__qualname__


def test_conditions_are_evaluated_before_the_contract_call() -> None:
    calls: list[str] = []

    def condition(name: str, result: bool) -> bool:
        calls.append(name)
        return result

    requires([condition("first", True), condition("second", False)])
    ensures([[condition("exit first", True), condition("exit second", False)], []])

    assert calls == ["first", "second", "exit first", "exit second"]


def test_expression_exceptions_propagate() -> None:
    error = RuntimeError("expression evaluation failed")

    def condition() -> bool:
        raise error

    with pytest.raises(RuntimeError) as caught:
        requires([condition()])

    assert caught.value is error


def test_an_entry_failure_prevents_the_method_body() -> None:
    calls: list[str] = []

    def run() -> None:
        requires([False, False])
        calls.append("body")

    with pytest.raises(ContractViolation):
        run()

    assert calls == []


def test_nested_calls_keep_their_own_entry_selection() -> None:
    def inner() -> None:
        requires([False, True])
        ensures([[False], [True]])

    requires([True, False])
    inner()
    ensures([[True], [False]])


def test_ensures_only_in_a_nested_call_does_not_adopt_the_outer_entry() -> None:
    def inner() -> None:
        ensures([[True]])

    requires([False, True])
    inner()
    ensures([[False], [True]])


def test_recursion_keeps_a_separate_selection_for_each_invocation() -> None:
    def recurse(depth: int) -> None:
        requires([depth % 2 == 0, depth % 2 == 1])
        if depth:
            recurse(depth - 1)
        ensures([[depth % 2 == 0], [depth % 2 == 1]])

    recurse(5)


def test_suspended_generators_keep_independent_entry_selections() -> None:
    def run(first: bool) -> Iterator[None]:
        requires([first, not first])
        yield
        ensures([[first], [not first]])

    first = run(True)
    second = run(False)
    next(first)
    next(second)
    with pytest.raises(StopIteration):
        next(first)
    with pytest.raises(StopIteration):
        next(second)


@pytest.mark.parametrize("failure", ["entry", "body", "exit", "early_return"])
def test_repeated_calls_do_not_reuse_state_after_failure_or_early_return(
    failure: str,
) -> None:
    def run(fail: bool) -> None:
        requires([fail and failure != "entry", not fail])
        if fail and failure == "body":
            raise RuntimeError("body failed")
        if fail and failure == "early_return":
            return
        ensures([[failure != "exit"], [True]])

    if failure == "early_return":
        run(True)
    else:
        with pytest.raises((ContractViolation, RuntimeError)):
            run(True)
    run(False)


@pytest.mark.parametrize(
    ("has_runnable", "expected"),
    [(False, NextRunnable.NONE), (True, NextRunnable.INIT_PROC)],
)
def test_pick_runnable_checks_the_corresponding_entry_and_exit_branch(
    model: GlobalVars, has_runnable: bool, expected: NextRunnable
) -> None:
    model.proc_table.has_runnable = has_runnable

    assert model.proc_table._pick_runnable(TaskLocalEnv()) is expected


@pytest.mark.parametrize("phase", ["requires", "ensures"])
@pytest.mark.parametrize("optimization", [(), ("-O",)])
def test_unhandled_failures_terminate_and_report_location_under_optimization(
    phase: str, optimization: tuple[str, ...]
) -> None:
    script = (
        "from framework.contracts import requires, ensures\n"
        "def rejected():\n"
        f"    requires([{phase == 'ensures'}])\n"
        "    ensures([[False]])\n"
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
    line = 3 if phase == "requires" else 4
    assert f"{phase} failed at <string>:{line} in rejected" in result.stderr


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
    model.ap_scheduler.state = state
    env = TaskLocalEnv()

    def no_operations(*args: Any, **kwargs: Any) -> None:
        pytest.fail("AP synchronization must not run after an entry failure")

    monkeypatch.setattr(LocalMultiTasks, "enable", no_operations)

    with pytest.raises(ContractViolation) as caught:
        model.ap_init_flow.start(env)

    assert caught.value.phase == "requires"
    assert caught.value.function == "ApInitFlow.start"
    assert model.bp_scheduler.state is SchedState.RUNNING
    assert model.ap_scheduler.state is state
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
    assert restored.smp_started.started is True
    assert model.bp_scheduler.state is SchedState.RUNNING
    assert model.initproc.state is ProcState.RUNNING


def test_ap_exit_failure_does_not_rewrite_the_loaded_snapshot(
    model: GlobalVars, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = GlobalVars()
    source.initproc.state = ProcState.RUNNABLE
    source.smp_started.started = True
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
