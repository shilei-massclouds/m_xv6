"""Context for Global vars"""

from dataclasses import dataclass, field

from flows.bp_init_flow import BpInitFlow
from drivers.console import Console


@dataclass
class GlobalVars:
    bp_init_flow: BpInitFlow = field(default_factory=BpInitFlow)
    console: Console = field(default_factory=Console)

    def reset(self):
        self.__init__()


gv = GlobalVars()
