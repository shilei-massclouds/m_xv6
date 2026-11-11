"""Application processor initialization flow."""

from dataclasses import dataclass

from framework.contention import DOMAINS
from framework.contracts import ensures, requires
from framework.engine import System, TaskLocalEnv
from framework.sync_primitives import LocalMultiTasks, Preemption, RemoteCpus
from kernel.proc import ProcState


@dataclass
class ApInitFlow(System):
    hartid: int = 1

    def start(self, env: TaskLocalEnv, **kwargs):
        """Placeholder for subsequent AP initialization derivations."""
        from global_vars import gv

        requires(self.hartid == 1)
        requires(gv.bp_init_flow.hartid == 0)
        requires(gv.initproc.state is ProcState.RUNNABLE)
        requires(gv.childproc.state is ProcState.UNUSED)
        requires(all(getattr(env.cv, domain) == 0 for domain in DOMAINS))

        LocalMultiTasks().enable(env.cv)
        # Note: ap_init_flow cannot be preempted.
        Preemption().disable(env.cv)
        RemoteCpus().enable(env.cv)

        ensures(self.hartid == 1)
        ensures(gv.bp_init_flow.hartid == 0)
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
