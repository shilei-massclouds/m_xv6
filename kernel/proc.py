"""Proc"""

from dataclasses import dataclass, field
from enum import Enum

from framework import snapshot_paths
from framework.contention import CPUSCOPE_CV, TASKPRIVATE_CV
from framework.contracts import ensures, requires
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
    state: ProcState = field(default=ProcState.UNUSED, repr=False)
    trampoline: Page = field(default_factory=Page)

    def _mapstack(self, env: TaskLocalEnv):
        self.drive(env, "page_allocator", "_kalloc")
        self.drive(env, "kernel_page_table", "_kvmmap")

    # Early preset in procinit
    def _preset(self, env: TaskLocalEnv):
        pass

    def _alloc(self, env: TaskLocalEnv):
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

    @visibility(TASKPRIVATE_CV)
    def _kexec(self, env: TaskLocalEnv):
        pass


class NextRunnable(Enum):
    INIT_PROC = "init_proc"
    CHILD_PROC = "child_proc"
    NONE = "none"


# ProcTable only has the references of `initproc` and `childproc`.
@dataclass
class ProcTable(System):
    has_runnable: bool = True

    def _mapstacks(self, env: TaskLocalEnv):
        self.drive(env, "initproc", "_mapstack")
        self.drive(env, "childproc", "_mapstack")

    # For procinit
    def _init(self, env: TaskLocalEnv):
        self.drive(env, "initproc", "_preset")
        self.drive(env, "childproc", "_preset")

    def pick_runnable(self, env: TaskLocalEnv) -> NextRunnable:
        with GuardSpinLock(env.cv, self):
            return self.drive(env, "self", "_pick_runnable")

    # Note: BlackBox
    def _pick_runnable(self, env: TaskLocalEnv) -> NextRunnable:
        requires(
            [
                not self.has_runnable,
                self.has_runnable,
            ]
        )

        # Drives other system.actions
        if self.has_runnable:
            nextp = NextRunnable.INIT_PROC
        else:
            nextp = NextRunnable.NONE

        ensures(
            [
                # NoRunnable -> NoRunnable
                [
                    (not self.has_runnable) and nextp is NextRunnable.NONE,
                ],
                # HasRunnable -> NoRunnable | HasRunnable
                [
                    (not self.has_runnable) and nextp is NextRunnable.INIT_PROC,
                    self.has_runnable and nextp is NextRunnable.INIT_PROC,
                ],
            ]
        )
        return nextp

    def put_runnable(self, env: TaskLocalEnv):
        with GuardSpinLock(env.cv, self):
            self.drive(env, "self", "_put_runnable")

    # Note: BlackBox
    def _put_runnable(self, env: TaskLocalEnv):
        requires(
            [
                not self.has_runnable,
                self.has_runnable,
            ]
        )

        # Drives other system.actions
        self.has_runnable = True

        ensures(
            [
                # NoRunnable -> HasRunnable
                [
                    self.has_runnable
                ],
                # HasRunnable -> HasRunnable
                [
                    self.has_runnable
                ],
            ]
        )


class SchedState(Enum):
    RUNNING = "running"
    SWITCHED = "switched"
    WFI = "wfi"


@dataclass
@visibility(CPUSCOPE_CV)
class Scheduler(System):
    state: SchedState = field(default=SchedState.RUNNING, repr=False)

    def _switch(self, env: TaskLocalEnv):
        self.state = SchedState.SWITCHED

    def _wfi(self, env: TaskLocalEnv):
        self.state = SchedState.WFI

    # Todo: check it only for bp_init_flow && ap_init_flow
    def schedule(self, env: TaskLocalEnv):
        from global_vars import gv

        LocalIrq().enable(env.cv)
        LocalIrq().disable(env.cv)
        if self is gv.bp_scheduler:
            gv.save_snapshot(
                snapshot_paths.BP_BEFORE_SCHEDULE_SNAPSHOT_PATH,
                checkpoint=snapshot_paths.BP_BEFORE_SCHEDULE_CHECKPOINT,
            )
        elif self is gv.ap_scheduler:
            gv.save_snapshot(
                snapshot_paths.AP_BEFORE_SCHEDULE_SNAPSHOT_PATH,
                checkpoint=snapshot_paths.AP_BEFORE_SCHEDULE_CHECKPOINT,
            )
        self.drive(env, "self", "_schedule")

    def _schedule(self, env: TaskLocalEnv):
        next_runnable = self.drive(env, "proc_table", "pick_runnable")
        match next_runnable:
            case NextRunnable.INIT_PROC | NextRunnable.CHILD_PROC:
                self.drive(env, "self", "_switch")
            case _:
                self.drive(env, "self", "_wfi")
