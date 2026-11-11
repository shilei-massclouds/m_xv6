"""FileSystem Inode Table"""

from dataclasses import dataclass

from framework.engine import System, TaskLocalEnv


@dataclass
class InodeTable(System):
    def _init(self, env: TaskLocalEnv):
        pass


@dataclass
class Inode(System):
    def _init(self, env: TaskLocalEnv):
        pass
