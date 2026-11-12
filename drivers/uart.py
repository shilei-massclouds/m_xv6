"""Uart Driver"""

from dataclasses import dataclass

from framework.engine import System, TaskLocalEnv, protected_by
from framework.sync_primitives import GuardLocalIrq, GuardPreemption


@dataclass
class Uart(System):
    def _init(self, env: TaskLocalEnv):
        pass

    def putc_sync(self, env: TaskLocalEnv):
        with GuardLocalIrq(env.cv), GuardPreemption(env.cv):
            self.drive(env, "self", "_putc_sync")

    @protected_by("io")
    def _putc_sync(self, env: TaskLocalEnv):
        pass
