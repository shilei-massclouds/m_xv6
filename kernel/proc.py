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
from structs.holder import Holder


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
    trapframe: Page | None = field(default=None, repr=False)
    pagetable: Page | None = field(default=None, repr=False)

    @visibility(TASKPRIVATE_CV)
    def _kexec(self, env: TaskLocalEnv):
        pass


@dataclass
class ProcTable(System):
    table: list[Proc] = field(default_factory=list, repr=False)

    # For procinit
    def _init(self, env: TaskLocalEnv):
        pass

    def _mapstacks(self, env: TaskLocalEnv):
        # Todo: preserve kernel pgtables according to number of procs
        self.drive(env, "page_allocator", "_kalloc")
        self.drive(env, "kernel_page_table", "_kvmmap")

    def _alloc(self, env: TaskLocalEnv):
        from global_vars import gv

        # Todo: check that `_mapstacks` has been called.
        proc = Proc()
        assert proc.state == ProcState.UNUSED, (
            "process must be unused before initialization"
        )
        proc.pid = self.drive(env, "pidgen", "_alloc")
        proc.state = ProcState.USED
        proc.trapframe = self.drive(env, "page_allocator", "_kalloc")
        proc.pagetable = self.drive(env, "page_allocator", "_kalloc")
        self.drive(env, "vm", "_mappages", pt=proc.trampoline)
        self.drive(env, "vm", "_mappages", pt=proc.pagetable)
        # Todo: p->cwd = namei("/");
        proc.state = ProcState.RUNNABLE
        self.drive(env, "proc_table", "put_runnable", task=proc)
        if gv.initproc is None:
            gv.initproc = proc

    def pick_runnable(self, env: TaskLocalEnv, **kwargs):
        with GuardSpinLock(env.cv, self):
            self.drive(env, "self", "_pick_runnable", **kwargs)

    # Note: BlackBox
    def _pick_runnable(self, env: TaskLocalEnv, **kwargs):
        requires(
            [
                # None
                not self.table,
                # One or More
                len(self.table) >= 1,
            ]
        )

        if self.table:
            nextp = self.table.pop(0)
        else:
            nextp = None

        ensures(
            [
                # None -> None
                [(not self.table) and nextp is None],
                # OneOrMore -> None | OneOrMore
                [
                    (not self.table) and nextp is not None,
                    len(self.table) >= 1 and nextp is not None,
                ],
            ]
        )

        kwargs["ret"].value = nextp

    def put_runnable(self, env: TaskLocalEnv, **kwargs):
        with GuardSpinLock(env.cv, self):
            self.drive(env, "self", "_put_runnable", **kwargs)

    # Note: BlackBox
    def _put_runnable(self, env: TaskLocalEnv, **kwargs):
        requires(
            [
                # None
                not self.table,
                # One or More
                len(self.table) >= 1,
            ]
        )

        self.table.append(kwargs["task"])

        ensures(
            [
                # None -> One
                [
                    len(self.table) == 1,
                ],
                # OneOrMore -> OneOrMore
                [
                    len(self.table) > 1,
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
        next_runnable = Holder[Proc]()
        self.drive(env, "proc_table", "pick_runnable", ret=next_runnable)
        match next_runnable.value:
            case None:
                self.drive(env, "self", "_wfi")
            case _:
                self.drive(env, "self", "_switch")
