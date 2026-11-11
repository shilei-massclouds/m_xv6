"""BootInitTask Origin"""

from dataclasses import dataclass

from framework.engine import TaskLocalEnv
from origins import Origin


@dataclass
class BpInitOrigin(Origin):
    def __init__(self):
        self.env = TaskLocalEnv()

    def start(self):
        self.drive(self.env, "bp_init_flow", "start")
