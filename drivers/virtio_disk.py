"""Console Device"""

from dataclasses import dataclass

from framework.engine import System, TaskLocalEnv


@dataclass
class VirtIODisk(System):
    def _init(self, env: TaskLocalEnv):
        pass
