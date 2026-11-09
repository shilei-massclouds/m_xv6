"""Kernel PageTable"""

from dataclasses import dataclass, field

from framework.engine import System
from structs.array import Array
from framework.sync_primitives import GuardBusyWaitIrqSavePreemption
from framework.sync_primitives import GuardSpinLock
from kernel.page_allocator import Page


@dataclass
class KernelPageTable(System):
    table: Page = field(default_factory=Page)

    def _init(self, env: TaskLocalEnv):
        self.table = self.drive(env, "page_allocator", "_kalloc")
        # Todo: multiple segments for mapping
        self.drive(env, "kernel_page_table", "_kvmmap")
        self.drive(env, "proc_table", "_mapstacks")

    def _kvmmap(self, env: TaskLocalEnv):
        self.drive(env, "vm", "_mappages", pt=self.table)


@dataclass
class VM(System):
    def _mappages(self, env: TaskLocalEnv, **kwargs):
        pass
