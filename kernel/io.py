"""IO Utility"""

from dataclasses import dataclass

from framework.engine import System, TaskLocalEnv
from framework.sync_primitives import GuardSpinLock


@dataclass
class IO(System):
    def _init(self, env: TaskLocalEnv):
        pass

    def printk(self, env: TaskLocalEnv, **kwargs):
        with GuardSpinLock(env.cv, self):
            # Todo: a contiguous sequence of bytes must be
            # output as an indivisible whole.
            # Rather than just `putc` one byte.
            self.drive(env, "console", "_sync_putc")
