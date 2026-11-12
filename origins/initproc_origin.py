"""Start the initial process flow from a BP or AP scheduling checkpoint."""

from dataclasses import dataclass, field
from typing import Literal

from framework import snapshot_paths
from framework.engine import TaskLocalEnv
from framework.sync_primitives import LocalMultiTasks, RemoteCpus
from global_vars import gv
from origins import Origin


@dataclass
class InitProcOrigin(Origin):
    source: Literal["bp", "ap"]
    env: TaskLocalEnv = field(default_factory=TaskLocalEnv, repr=False)

    def start(self):
        if self.source == "bp":
            path = snapshot_paths.BP_BEFORE_SCHEDULE_SNAPSHOT_PATH
            checkpoint = snapshot_paths.BP_BEFORE_SCHEDULE_CHECKPOINT
        elif self.source == "ap":
            path = snapshot_paths.AP_BEFORE_SCHEDULE_SNAPSHOT_PATH
            checkpoint = snapshot_paths.AP_BEFORE_SCHEDULE_CHECKPOINT
        else:
            raise ValueError(f"unknown InitProc source: {self.source!r}")

        gv.load_snapshot(path, expected_checkpoint=checkpoint)
        LocalMultiTasks().enable(self.env.cv)
        RemoteCpus().enable(self.env.cv)
        self.drive(self.env, "initproc_flow", "start")
