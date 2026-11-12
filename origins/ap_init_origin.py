"""Start AP derivation from the BP initialization checkpoint."""

from dataclasses import dataclass, field

from framework import snapshot_paths
from framework.engine import TaskLocalEnv
from framework.sync_primitives import Preemption, RemoteCpus
from global_vars import gv
from origins import Origin


@dataclass
class ApInitOrigin(Origin):
    env: TaskLocalEnv = field(default_factory=TaskLocalEnv, repr=False)

    def start(self):
        gv.load_snapshot(
            snapshot_paths.BP_INITIALIZED_SNAPSHOT_PATH,
            expected_checkpoint=snapshot_paths.BP_INITIALIZED_CHECKPOINT,
        )

        # Note: ap_init_flow cannot be preempted.
        Preemption().disable(self.env.cv)
        RemoteCpus().enable(self.env.cv)

        self.drive(self.env, "ap_init_flow", "start")
