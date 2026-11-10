"""Current Hart"""

from dataclasses import dataclass, field

from framework.engine import System
from structs.array import Array
from framework.sync_primitives import GuardBusyWaitIrqSavePreemption
from framework.sync_primitives import GuardSpinLock
from kernel.page_allocator import Page


@dataclass
class CurrentHart(System):
    def _set_satp(self, env: TaskLocalEnv):
        pass

    def _set_stvec(self, env: TaskLocalEnv):
        pass
