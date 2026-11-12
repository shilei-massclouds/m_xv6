"""Initial process flow skeleton."""

from dataclasses import dataclass

from framework.engine import System, TaskLocalEnv


@dataclass
class InitProcFlow(System):
    def start(self, env: TaskLocalEnv, **kwargs):
        pass
