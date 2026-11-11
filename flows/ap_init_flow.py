"""Application processor initialization flow."""

from dataclasses import dataclass

from framework.engine import System, TaskLocalEnv
from framework.sync_primitives import LocalMultiTasks, Preemption, RemoteCpus


@dataclass
class ApInitFlow(System):
    hartid: int = 1

    def start(self, env: TaskLocalEnv, **kwargs):
        """Placeholder for subsequent AP initialization derivations."""
        LocalMultiTasks().enable(env.cv)
        # Note: ap_init_flow cannot be preempted.
        Preemption().disable(env.cv)
        RemoteCpus().enable(env.cv)
