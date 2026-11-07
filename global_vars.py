"""Context for Global vars"""

from dataclasses import dataclass, field

from flows.bp_init_flow import BpInitFlow
from drivers.console import Console
from drivers.uart import Uart
from drivers.devsw import DevSW


@dataclass
class GlobalVars:
    bp_init_flow: BpInitFlow = field(default_factory=BpInitFlow)
    console: Console = field(default_factory=Console)
    uart: Uart = field(default_factory=Uart)
    devsw: DevSW = field(default_factory=DevSW)

    def reset(self):
        self.__init__()


gv = GlobalVars()
