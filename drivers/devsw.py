"""DevSW Table"""

from dataclasses import dataclass

from framework.engine import System, TaskLocalEnv


@dataclass
class DevSW(System):
    def _register(self, env: TaskLocalEnv, **kwargs):
        pass
