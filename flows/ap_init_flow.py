"""Application processor initialization flow."""

from dataclasses import dataclass

from flows import CpuFlow, CpuFlowState
from framework.contracts import ensures, requires
from framework.engine import TaskLocalEnv
from framework.sync_primitives import LocalMultiTasks


@dataclass
class ApInitFlow(CpuFlow):
    hartid: int = 1

    def start(self, env: TaskLocalEnv, **kwargs):
        """Placeholder for subsequent AP initialization derivations."""
        requires([self.state is CpuFlowState.RUNNING])

        self.drive(env, "smp_started", "wait")

        self.drive(env, "io", "printk")
        self.drive(env, "ap_hart", "_set_satp")  # kvminithart
        self.drive(env, "ap_hart", "_set_stvec")  # trapinithart
        self.drive(env, "plic", "_init_hart", hartid=self.hartid)  # plicinit

        LocalMultiTasks().enable(env.cv)
        self.drive(env, "self", "schedule")  # scheduler

        ensures(
            [
                [
                    self.state is CpuFlowState.SWITCHED,
                    self.state is CpuFlowState.WFI,
                ]
            ]
        )

    def wait_smp_enabled(self, env: TaskLocalEnv):
        requires([self.state is CpuFlowState.WAITING])

        ensures(
            [
                [
                    self.state is CpuFlowState.WAITING,
                    self.state is CpuFlowState.RUNNING,
                ]
            ]
        )
