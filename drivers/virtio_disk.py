"""Console Device"""

from dataclasses import dataclass, field

from framework.engine import System
from structs.array import Array


@dataclass
class VirtIODisk(System):
    def _init(self, env: TaskLocalEnv):
        pass
