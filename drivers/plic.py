"""Plic Driver"""

from dataclasses import dataclass, field

from framework.engine import System
from structs.array import Array


@dataclass
class Plic(System):
    def _init(self, env: TaskLocalEnv):
        pass

    def _init_hart(self, env: TaskLocalEnv, **kwargs):
        pass
