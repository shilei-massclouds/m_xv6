"""Page Allocator"""

from dataclasses import dataclass

from framework.engine import System, TaskLocalEnv
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

    def _kalloc(self, env: TaskLocalEnv):
        return Page()


@dataclass
class Page(System):
    pass
