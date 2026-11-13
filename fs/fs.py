"""FileSystem"""

from dataclasses import dataclass

from framework.contention import TASKPRIVATE_CV
from framework.engine import System, TaskLocalEnv, visibility


# Only belong to InitProc
@dataclass
class FileSystem(System):
    @visibility(TASKPRIVATE_CV)
    def _init(self, env: TaskLocalEnv):
        pass
