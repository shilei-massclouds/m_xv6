"""Proc"""

from dataclasses import dataclass, field
from enum import Enum

from framework.contention import CPUSCOPE_CV
from framework.engine import System, TaskLocalEnv, visibility
from framework.sync_primitives import (
    GuardSpinLock,
    LocalIrq,
)
from kernel.page_allocator import Page


class ProcState(Enum):
    UNUSED = "unused"
    USED = "used"
    SLEEPING = "sleeping"
    RUNNABLE = "runnable"
    RUNNING = "running"
    ZOMBIE = "zombie"


@dataclass
class PidGen(System):
    def _alloc(self, env: TaskLocalEnv):
        pass


@dataclass
class Proc(System):
    pid: int = field(default=0)
    state: ProcState = field(default=ProcState.UNUSED)
    trampoline: Page = field(default_factory=Page)

    def _mapstack(self, env: TaskLocalEnv):
        self.drive(env, "page_allocator", "_kalloc")
        self.drive(env, "kernel_page_table", "_kvmmap")

    def _preset(self, env: TaskLocalEnv):
        pass

    def _init(self, env: TaskLocalEnv):
        # Todo: check that `_mapstack` has been called.
        assert self.state == ProcState.UNUSED, (
            "process must be unused before initialization"
        )
        self.pid = self.drive(env, "pidgen", "_alloc")
        self.state = ProcState.USED
        self.trapframe = self.drive(env, "page_allocator", "_kalloc")
        self.pagetable = self.drive(env, "page_allocator", "_kalloc")
        self.drive(env, "vm", "_mappages", pt=self.trampoline)
        self.drive(env, "vm", "_mappages", pt=self.pagetable)
        # Todo: p->cwd = namei("/");
        self.state = ProcState.RUNNABLE


class NextRunnable(Enum):
    INIT_PROC = "init_proc"
    CHILD_PROC = "child_proc"
    NONE = "none"


# ProcTable only has the references of `initproc` and `childproc`.
@dataclass
class ProcTable(System):
    next_runnable: NextRunnable = field(default=NextRunnable.INIT_PROC)

    def _mapstacks(self, env: TaskLocalEnv):
        self.drive(env, "initproc", "_mapstack")
        self.drive(env, "childproc", "_mapstack")

    def _init(self, env: TaskLocalEnv):
        self.drive(env, "initproc", "_preset")
        self.drive(env, "childproc", "_preset")

    def pick_next(self, env: TaskLocalEnv):
        with GuardSpinLock(env.cv, self):
            return self.drive(env, "proc_table", "_pick_next")

    def _pick_next(self, env: TaskLocalEnv):
        return self.next_runnable


@dataclass
@visibility(CPUSCOPE_CV)
class Scheduler(System):
    def _switch(self, env: TaskLocalEnv):
        pass

    def _wfi(self, env: TaskLocalEnv):
        pass

    # Todo: check it only for bp_init_flow
    def schedule(self, env: TaskLocalEnv):
        LocalIrq().enable(env.cv)
        LocalIrq().disable(env.cv)
        self.drive(env, "self", "_schedule")

    def _schedule(self, env: TaskLocalEnv):
        next_runnable = self.drive(env, "proc_table", "pick_next")
        match next_runnable:
            case NextRunnable.INIT_PROC | NextRunnable.CHILD_PROC:
                self.drive(env, "self", "_switch")
            case _:
                self.drive(env, "self", "_wfi")
