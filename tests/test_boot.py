"""Boot initialization creates processes on demand and retains initproc."""

from collections.abc import Iterator

import pytest

from framework.engine import TaskLocalEnv
from global_vars import gv
from kernel.page_allocator import Page
from kernel.proc import ProcState
from origins.bp_init_origin import BpInitOrigin


@pytest.fixture(autouse=True)
def reset_globals() -> Iterator[None]:
    gv.reset()
    yield
    gv.reset()


def test_boot_initializes_the_first_process() -> None:
    origin = BpInitOrigin()
    assert gv.initproc is None
    assert gv.proc_table.table == []
    assert not hasattr(gv, "childproc")

    origin.start()

    assert gv.initproc is not None
    assert gv.initproc.state is ProcState.RUNNABLE
    assert isinstance(gv.initproc.trapframe, Page)
    assert isinstance(gv.initproc.pagetable, Page)
    assert not hasattr(gv, "childproc")
    assert origin.env.cv.local_irq == 0
    assert origin.env.cv.local_tasks == 0
    assert origin.env.cv.remote_irq == 1
    assert origin.env.cv.remote_tasks == 1
    assert origin.env.depth == 0
    assert origin.env.signal_queues == []


def test_later_allocations_create_distinct_processes_and_preserve_initproc() -> None:
    env = TaskLocalEnv()
    gv.proc_table._alloc(env)
    first = gv.initproc
    assert first is not None
    assert len(gv.proc_table.table) == 1
    assert gv.proc_table.table[0] is first

    gv.proc_table._alloc(env)

    assert gv.initproc is first
    assert len(gv.proc_table.table) == 2
    second = gv.proc_table.table[1]
    assert second is not first
    assert second.state is ProcState.RUNNABLE
    assert isinstance(second.trapframe, Page)
    assert isinstance(second.pagetable, Page)
    assert second.trapframe is not first.trapframe
    assert second.pagetable is not first.pagetable
    assert not hasattr(gv, "childproc")
    assert env.depth == 0
    assert env.signal_queues == []


def test_reset_clears_the_initproc_reference_and_runnable_queue() -> None:
    gv.proc_table._alloc(TaskLocalEnv())
    first = gv.initproc

    gv.reset()

    assert gv.initproc is None
    assert gv.proc_table.table == []
    gv.proc_table._alloc(TaskLocalEnv())
    assert gv.initproc is not None
    assert gv.initproc is not first
