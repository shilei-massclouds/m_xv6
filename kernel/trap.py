"""Trap Utility"""

from dataclasses import dataclass

from framework.engine import System, TaskLocalEnv


@dataclass
class Trap(System):
    def _init(self, env: TaskLocalEnv):
        pass
