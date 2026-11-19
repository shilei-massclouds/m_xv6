"""Context for Global vars"""

from dataclasses import dataclass, field

from flows.model_specs import new_model
from framework.engine import System

MODEL_VERSION = 10


@dataclass
class GlobalVars:
    bp_init_flow: System = field(default_factory=lambda: new_model("BpInitFlow"))
    ap_init_flow: System = field(default_factory=lambda: new_model("ApInitFlow"))
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
    first_proc_flag: System = field(default_factory=lambda: new_model("FirstProcFlag"))
    fs: System = field(default_factory=lambda: new_model("FileSystem"))

    def reset(self):
        """Restore defaults, including removing dynamically added attributes."""
        self.__dict__ = vars(type(self)())


gv = GlobalVars()
