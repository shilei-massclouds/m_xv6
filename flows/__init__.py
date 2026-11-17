"""Flow basic elements."""

from dataclasses import dataclass, field
from enum import Enum

from framework.contention import CPUSCOPE_CV
from framework.engine import System, TaskLocalEnv, visibility
from framework.sync_primitives import LocalIrq
from kernel.proc import Proc
from structs.holder import Holder


class CpuFlowState(Enum):
    RUNNING = "running"
    WAITING = "waiting"
    SWITCHED = "switched"
    WFI = "wfi"


@dataclass
@visibility(CPUSCOPE_CV)
class CpuFlow(System):
    state: CpuFlowState = field(default=CpuFlowState.RUNNING, repr=False)

    def schedule(self, env: TaskLocalEnv):
        LocalIrq().enable(env.cv)
        LocalIrq().disable(env.cv)
        self.drive(env, "self", "_schedule")

    def _schedule(self, env: TaskLocalEnv):
        next_runnable = Holder[Proc]()
        self.drive(env, "proc_table", "pick_runnable", ret=next_runnable)
        match next_runnable.value:
            case None:
                self.state = CpuFlowState.WFI
            case _:
                self.state = CpuFlowState.SWITCHED
