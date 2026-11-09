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


@dataclass
class ProcTable(System):
    element: Proc = field(default_factory=Proc)

    def _mapstacks(self, env: TaskLocalEnv):
        self.drive(env, "proc_table.element", "_mapstack")
