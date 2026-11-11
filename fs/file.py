"""FileSystem File Table"""

from dataclasses import dataclass

from framework.engine import System, TaskLocalEnv


@dataclass
class FileTable(System):
    def _init(self, env: TaskLocalEnv):
        pass


@dataclass
class File(System):
    def _init(self, env: TaskLocalEnv):
        pass
