"""Plic Driver"""

from dataclasses import dataclass

from framework.contention import CPUSCOPE_CV
from framework.engine import System, TaskLocalEnv, visibility


@dataclass
class Plic(System):
    def _init(self, env: TaskLocalEnv):
        pass

    @visibility(CPUSCOPE_CV)
    def _init_hart(self, env: TaskLocalEnv, **kwargs):
        pass
