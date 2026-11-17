"""BootInitTask Origin"""

from dataclasses import dataclass, field

from framework.engine import TaskLocalEnv
from origins import Origin


@dataclass
class BpInitOrigin(Origin):
    env: TaskLocalEnv = field(default_factory=TaskLocalEnv, repr=False)

    def start(self):
        self.drive(self.env, "bp_init_flow", "_start")
