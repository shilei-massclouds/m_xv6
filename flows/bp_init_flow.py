"""BootInitTask Flow"""

from dataclasses import dataclass

from flows import CpuFlow, CpuFlowState
from framework.contracts import ensures, requires
from framework.engine import TaskLocalEnv
from framework.sync_primitives import LocalMultiTasks, Preemption, RemoteCpus


@dataclass
class BpInitFlow(CpuFlow):
    hartid: int = 0

    def _start(self, env: TaskLocalEnv, **kwargs):
        requires([self.state is CpuFlowState.RUNNING])

        self.drive(env, "console", "_init")  # consoleinit
        self.drive(env, "io", "_init")  # printkinit
        self.drive(env, "io", "printk")
        self.drive(env, "page_allocator", "_free_range")  # kinit
        self.drive(env, "kernel_page_table", "_init")  # kvminit
        self.drive(env, "bp_hart", "_set_satp")  # kvminithart
        self.drive(env, "proc_table", "_init")  # procinit
        self.drive(env, "trap", "_init")  # trapinit
        self.drive(env, "bp_hart", "_set_stvec")  # trapinithart
        self.drive(env, "plic", "_init")  # plicinit
        self.drive(env, "plic", "_init_hart", hartid=self.hartid)  # plicinit
        self.drive(env, "bcache", "_init")  # binit
        self.drive(env, "itable", "_init")  # iinit
        self.drive(env, "ftable", "_init")  # fileinit
        self.drive(env, "vdisk", "_init")  # virtio_disk_init
        self.drive(env, "self", "userinit")  # userinit

        # Note: bp_init_flow cannot be preempted.
        Preemption().disable(env.cv)
        RemoteCpus().enable(env.cv)

        # Notify other harts to continue.
        self.drive(env, "smp_started", "notify")

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

    def userinit(self, env: TaskLocalEnv):
        self.drive(env, "proc_table", "_alloc")
