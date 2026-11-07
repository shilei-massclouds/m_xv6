"""BootInitTask Origin"""

from collections import deque
from dataclasses import dataclass
from framework.engine import TaskLocalEnv, System
from origins import Origin


@dataclass
class BpInitOrigin(Origin):
    def __init__(self):
        self.env = TaskLocalEnv()

    def start(self):
        self.drive(self.env, "bp_init_flow", "start")
