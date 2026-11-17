"""Proc"""

from dataclasses import dataclass, field
from enum import Enum

from framework.contention import TASKPRIVATE_CV
from framework.contracts import ensures, requires
from framework.engine import System, TaskLocalEnv, visibility
from framework.sync_primitives import GuardSpinLock
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
