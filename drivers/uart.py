"""Uart Driver"""

from dataclasses import dataclass, field
from framework.engine import System

@dataclass
class Uart(System):
    def _init(self, env: TaskLocalEnv):
        pass
