"""Proc"""

from dataclasses import dataclass, field

from framework.engine import System
from structs.array import Array
from framework.sync_primitives import GuardBusyWaitIrqSavePreemption
from framework.sync_primitives import GuardSpinLock


@dataclass
class Proc(System):
    def _mapstack(self, env: TaskLocalEnv):
        self.drive(env, "page_allocator", "_kalloc")
        self.drive(env, "kernel_page_table", "_kvmmap")

    def _init(self, env: TaskLocalEnv):
        # Todo: makesure that `_mapstack` has been called.
        pass


@dataclass
class ProcTable(System):
    element: Proc = field(default_factory=Proc)

    def _mapstacks(self, env: TaskLocalEnv):
        self.drive(env, "proc_table.element", "_mapstack")

    def _init(self, env: TaskLocalEnv):
        self.drive(env, "proc_table.element", "_init")

    def _schedule(self, env: TaskLocalEnv):
        pass
