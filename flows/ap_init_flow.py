"""Application processor initialization flow."""

from dataclasses import dataclass

from framework.contracts import ensures, requires
from framework.engine import System, TaskLocalEnv
from kernel.proc import SchedState


@dataclass
class ApInitFlow(System):
    hartid: int = 1

    def start(self, env: TaskLocalEnv, **kwargs):
        """Placeholder for subsequent AP initialization derivations."""
        from global_vars import gv

        requires(gv.ap_scheduler.state is SchedState.RUNNING)

        self.drive(env, "smp_started", "wait")

        self.drive(env, "ap_scheduler", "schedule")  # scheduler

        ensures(gv.ap_scheduler.state in [SchedState.SWITCHED, SchedState.WFI])
