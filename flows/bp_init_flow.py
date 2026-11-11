"""BootInitTask Flow"""

from dataclasses import dataclass

from framework.engine import System, TaskLocalEnv
from framework.sync_primitives import LocalMultiTasks, Preemption, RemoteCpus


@dataclass
class BpInitFlow(System):
    hartid: int = 0

    def start(self, env: TaskLocalEnv, **kwargs):
        self.drive(env, "console", "_init")  # consoleinit
        self.drive(env, "io", "_init")  # printkinit
        self.drive(env, "io", "printk")
        self.drive(env, "page_allocator", "_free_range")  # kinit
        self.drive(env, "kernel_page_table", "_init")  # kvminit
        self.drive(env, "current_hart", "_set_satp")  # kvminithart
        self.drive(env, "proc_table", "_init")  # procinit
        self.drive(env, "trap", "_init")  # trapinit
        self.drive(env, "current_hart", "_set_stvec")  # trapinithart
        self.drive(env, "plic", "_init")  # plicinit
        self.drive(env, "plic", "_init_hart", hartid=self.hartid)  # plicinit
        self.drive(env, "bcache", "_init")  # binit
        self.drive(env, "itable", "_init")  # iinit
        self.drive(env, "ftable", "_init")  # fileinit
        self.drive(env, "vdisk", "_init")  # virtio_disk_init
        self.drive(env, "initproc", "_init")  # userinit

        #
        # Todo: notify other harts by atomic
        #
        LocalMultiTasks().enable(env.cv)
        # Note: bp_init_flow cannot be preempted.
        Preemption().disable(env.cv)
        RemoteCpus().enable(env.cv)

        self.drive(env, "scheduler", "schedule")  # scheduler
