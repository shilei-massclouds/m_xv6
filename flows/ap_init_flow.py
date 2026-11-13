"""Application processor initialization flow."""

from dataclasses import dataclass

from framework.contracts import ensures, requires
from framework.engine import System, TaskLocalEnv
from framework.sync_primitives import LocalMultiTasks
from kernel.proc import SchedState


@dataclass
class ApInitFlow(System):
    hartid: int = 1

    def start(self, env: TaskLocalEnv, **kwargs):
        """Placeholder for subsequent AP initialization derivations."""
        from global_vars import gv

        requires([gv.ap_scheduler.state is SchedState.RUNNING])

        self.drive(env, "smp_started", "wait")
        self.drive(env, "io", "printk")
        self.drive(env, "ap_hart", "_set_satp")  # kvminithart
        self.drive(env, "ap_hart", "_set_stvec")  # trapinithart
        self.drive(env, "plic", "_init_hart", hartid=self.hartid)  # plicinit

        LocalMultiTasks().enable(env.cv)
        self.drive(env, "ap_scheduler", "schedule")  # scheduler

        ensures(
            [
                [
                    gv.ap_scheduler.state is SchedState.SWITCHED,
                    gv.ap_scheduler.state is SchedState.WFI,
                ]
            ]
        )
