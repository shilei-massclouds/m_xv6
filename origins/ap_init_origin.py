"""Start independent AP derivation with a fresh task-local environment."""

from dataclasses import dataclass, field

from framework.engine import TaskLocalEnv
from framework.sync_primitives import Preemption, RemoteCpus
from origins import Origin


@dataclass
class ApInitOrigin(Origin):
    env: TaskLocalEnv = field(
        default_factory=lambda: TaskLocalEnv(name="ap"), repr=False
    )

    def start(self):
        # Note: ap_init_flow cannot be preempted.
        Preemption().disable(self.env.cv)
        RemoteCpus().enable(self.env.cv)

        self.drive(self.env, "ap_init_flow", "_start")
