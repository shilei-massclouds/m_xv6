"""Plic Driver"""

from dataclasses import dataclass

from framework.engine import System, TaskLocalEnv


@dataclass
class Plic(System):
    def _init(self, env: TaskLocalEnv):
        pass

    def _init_hart(self, env: TaskLocalEnv, **kwargs):
        pass
