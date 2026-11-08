"""Uart Driver"""

from dataclasses import dataclass, field
from framework.engine import System
from framework.sync_primitives import GuardLocalIrq, GuardPreemption

@dataclass
class Uart(System):
    def _init(self, env: TaskLocalEnv):
        pass

    def putc_sync(self, env: TaskLocalEnv):
        with GuardLocalIrq(env.cv):
            with GuardPreemption(env.cv):
                self.drive(env, "uart", "_putc_sync")

    def _putc_sync(self, env: TaskLocalEnv):
        pass
