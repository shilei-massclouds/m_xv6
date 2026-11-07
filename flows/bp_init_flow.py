"""BootInitTask Flow"""

from dataclasses import dataclass
from framework.engine import System


@dataclass
class BpInitFlow(System):
    def start(self, env: TaskLocalEnv, **kwargs):
        self.drive(env, "console", "init")
