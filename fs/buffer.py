"""FileSystem Buffer Cache"""

from dataclasses import dataclass, field

from framework.engine import System
from structs.array import Array
from framework.sync_primitives import GuardBusyWaitIrqSavePreemption
from framework.sync_primitives import GuardSpinLock
from kernel.page_allocator import Page


@dataclass
class BufferCache(System):
    def _init(self, env: TaskLocalEnv):
        pass
