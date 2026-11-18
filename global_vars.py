"""Context for Global vars"""

from dataclasses import dataclass, field

from flows.ap_init_flow import ApInitFlow
from flows.bp_init_flow import BpInitFlow
from flows.initproc_flow import InitProcFlow
from flows.model_specs import new_model
from framework.engine import System

MODEL_VERSION = 9


@dataclass
class GlobalVars:
    bp_init_flow: System = field(default_factory=BpInitFlow)
    ap_init_flow: System = field(default_factory=ApInitFlow)
    initproc_flow: System = field(default_factory=InitProcFlow)
    console: System = field(default_factory=lambda: new_model("Console"))
    io: System = field(default_factory=lambda: new_model("IO"))
    uart: System = field(default_factory=lambda: new_model("Uart"))
    devsw: System = field(default_factory=lambda: new_model("DevSW"))
    page_allocator: System = field(default_factory=lambda: new_model("PageAllocator"))
    kernel_page_table: System = field(
        default_factory=lambda: new_model("KernelPageTable")
    )
    vm: System = field(default_factory=lambda: new_model("VM"))
    proc_table: System = field(default_factory=lambda: new_model("ProcTable"))
    bp_hart: System = field(default_factory=lambda: new_model("CurrentHart"))
    ap_hart: System = field(default_factory=lambda: new_model("CurrentHart"))
    trap: System = field(default_factory=lambda: new_model("Trap"))
    plic: System = field(default_factory=lambda: new_model("Plic"))
    bcache: System = field(default_factory=lambda: new_model("BufferCache"))
    itable: System = field(default_factory=lambda: new_model("InodeTable"))
    ftable: System = field(default_factory=lambda: new_model("FileTable"))
    vdisk: System = field(default_factory=lambda: new_model("VirtIODisk"))
    initproc: System | None = None
    pidgen: System = field(default_factory=lambda: new_model("PidGen"))
    smp_started: System = field(default_factory=lambda: new_model("SmpStarted"))
    first_proc: System = field(default_factory=lambda: new_model("FirstProc"))
    fs: System = field(default_factory=lambda: new_model("FileSystem"))

    def reset(self):
        self.__init__()


gv = GlobalVars()
