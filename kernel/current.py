"""Current Hart"""

from dataclasses import dataclass

from framework.engine import System, TaskLocalEnv


@dataclass
class CurrentHart(System):
    def _set_satp(self, env: TaskLocalEnv):
        pass

    def _set_stvec(self, env: TaskLocalEnv):
        pass
