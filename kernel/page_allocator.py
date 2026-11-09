"""Page Allocator"""

from dataclasses import dataclass, field

from framework.engine import System
from structs.array import Array
from framework.sync_primitives import GuardBusyWaitIrqSavePreemption
from framework.sync_primitives import GuardSpinLock


@dataclass
class PageAllocator(System):
    def _free_range(self, env: TaskLocalEnv):
        # Todo: kfree in loop
        self.drive(env, "page_allocator", "kfree")

    def kfree(self, env: TaskLocalEnv):
        with GuardSpinLock(env.cv, self):
            self.drive(env, "page_allocator", "_kfree")

    def _kfree(self, env: TaskLocalEnv):
        pass
