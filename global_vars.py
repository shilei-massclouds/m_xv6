"""Context for Global vars"""

from dataclasses import dataclass, field

from flows.bp_init_flow import BpInitFlow
from drivers.console import Console
from kernel.io import IO
from kernel.page_allocator import PageAllocator
from drivers.uart import Uart
from drivers.devsw import DevSW


@dataclass
class GlobalVars:
    bp_init_flow: BpInitFlow = field(default_factory=BpInitFlow)
    console: Console = field(default_factory=Console)
    io: IO = field(default_factory=IO)
    uart: Uart = field(default_factory=Uart)
    devsw: DevSW = field(default_factory=DevSW)
    page_allocator: PageAllocator = field(default_factory=PageAllocator)

    def reset(self):
        self.__init__()


gv = GlobalVars()
