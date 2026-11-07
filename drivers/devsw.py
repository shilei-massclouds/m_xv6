"""DevSW Table"""

from dataclasses import dataclass, field
from framework.engine import System

@dataclass
class DevSW(System):
    def _register(self, env: TaskLocalEnv, **kwargs):
        pass

