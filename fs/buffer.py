"""FileSystem Buffer Cache"""

from dataclasses import dataclass

from framework.engine import System, TaskLocalEnv


@dataclass
class BufferCache(System):
    def _init(self, env: TaskLocalEnv):
        pass
