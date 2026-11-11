"""BootInitTask Flow"""

from dataclasses import dataclass

from framework import snapshot_paths
from framework.contention import DOMAINS
from framework.contracts import ensures, requires
from framework.engine import System, TaskLocalEnv
from framework.sync_primitives import LocalMultiTasks, Preemption, RemoteCpus
from kernel.proc import ProcState


@dataclass
class BpInitFlow(System):
    hartid: int = 0

    def start(self, env: TaskLocalEnv, **kwargs):
        from global_vars import gv

        requires(self.hartid == 0)
        requires(gv.initproc.state is ProcState.UNUSED)
        requires(gv.childproc.state is ProcState.UNUSED)
        requires(all(getattr(env.cv, domain) == 0 for domain in DOMAINS))

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
        self.drive(env, "initproc", "_init")  # userinit

        gv.save_snapshot(
            snapshot_paths.BP_INITIALIZED_SNAPSHOT_PATH,
            checkpoint=snapshot_paths.BP_INITIALIZED_CHECKPOINT,
        )

        #
        # Todo: notify other harts by atomic
        #
        LocalMultiTasks().enable(env.cv)
        # Note: bp_init_flow cannot be preempted.
        Preemption().disable(env.cv)
        RemoteCpus().enable(env.cv)

        self.drive(env, "bp_scheduler", "schedule")  # scheduler

        ensures(self.hartid == 0)
        ensures(gv.initproc.state is ProcState.RUNNABLE)
        ensures(gv.childproc.state is ProcState.UNUSED)
        ensures(
            (
                env.cv.local_irq,
                env.cv.local_tasks,
                env.cv.remote_irq,
                env.cv.remote_tasks,
            )
            == (0, 0, 1, 1)
        )
