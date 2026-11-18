"""Start the initial process flow on the GV supplied by its FlowRun."""

from dataclasses import dataclass, field
from typing import Literal

from framework.engine import TaskLocalEnv
from framework.sync_primitives import LocalMultiTasks, RemoteCpus
from origins import Origin


@dataclass
class InitProcOrigin(Origin):
    source: Literal["bp", "ap"]
    env: TaskLocalEnv = field(
        default_factory=lambda: TaskLocalEnv(name="initproc"), repr=False
    )

    def start(self):
        LocalMultiTasks().enable(self.env.cv)
        RemoteCpus().enable(self.env.cv)
        self.drive(self.env, "initproc_flow", "start")
