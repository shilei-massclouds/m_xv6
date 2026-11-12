"""Regression coverage for boot initialization and process state checks."""

from collections.abc import Iterator

import pytest

from framework.engine import TaskLocalEnv
from global_vars import gv
from kernel.proc import Proc, ProcState
from origins.bp_init_origin import BpInitOrigin


@pytest.fixture(autouse=True)
def reset_globals() -> Iterator[None]:
    gv.reset()
    yield
    gv.reset()


def test_boot_initializes_the_first_process() -> None:
    origin = BpInitOrigin()

    origin.start()

    assert gv.initproc.state is ProcState.RUNNABLE
    assert gv.childproc.state is ProcState.UNUSED
    assert origin.env.cv.local_irq == 0
    assert origin.env.cv.local_tasks == 0
    assert origin.env.cv.remote_irq == 1
    assert origin.env.cv.remote_tasks == 1
    assert origin.env.depth == 0
    assert origin.env.signal_queues == []


@pytest.mark.parametrize(
    "state",
    [
        ProcState.USED,
        ProcState.SLEEPING,
        ProcState.RUNNABLE,
        ProcState.RUNNING,
        ProcState.ZOMBIE,
    ],
)
def test_proc_initialization_rejects_an_in_use_process(state: ProcState) -> None:
    proc = Proc(state=state)

    with pytest.raises(AssertionError, match="process must be unused"):
        proc._alloc(TaskLocalEnv())

    assert proc.state is state
    assert proc.pid == 0
