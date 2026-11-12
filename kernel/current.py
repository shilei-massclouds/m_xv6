"""Current Hart"""

from dataclasses import dataclass

from framework.contention import CPUSCOPE_CV
from framework.engine import System, TaskLocalEnv, visibility


@dataclass
@visibility(CPUSCOPE_CV)
class CurrentHart(System):
    def _set_satp(self, env: TaskLocalEnv):
        pass

    def _set_stvec(self, env: TaskLocalEnv):
        pass
