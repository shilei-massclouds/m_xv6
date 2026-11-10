"""Context for Global vars"""

from dataclasses import dataclass, field

from flows.bp_init_flow import BpInitFlow
from drivers.console import Console
from kernel.io import IO
from kernel.page_allocator import PageAllocator
from kernel.vm import KernelPageTable, VM
from kernel.proc import ProcTable, Proc
from kernel.current import CurrentHart
from kernel.trap import Trap
from drivers.uart import Uart
from drivers.devsw import DevSW
from drivers.plic import Plic
from drivers.virtio_disk import VirtIODisk
from fs.buffer import BufferCache
from fs.inode import InodeTable
from fs.file import FileTable


@dataclass
class GlobalVars:
    bp_init_flow: BpInitFlow = field(default_factory=BpInitFlow)
    console: Console = field(default_factory=Console)
    io: IO = field(default_factory=IO)
    uart: Uart = field(default_factory=Uart)
    devsw: DevSW = field(default_factory=DevSW)
    page_allocator: PageAllocator = field(default_factory=PageAllocator)
    kernel_page_table: KernelPageTable = field(default_factory=KernelPageTable)
    vm: VM = field(default_factory=VM)
    proc_table: ProcTable = field(default_factory=ProcTable)
    current_hart: CurrentHart = field(default_factory=CurrentHart)
    trap: Trap = field(default_factory=Trap)
    plic: Plic = field(default_factory=Plic)
    bcache: BufferCache = field(default_factory=BufferCache)
    itable: InodeTable = field(default_factory=InodeTable)
    ftable: FileTable = field(default_factory=FileTable)
    vdisk: VirtIODisk = field(default_factory=VirtIODisk)
    initproc: Proc = field(default_factory=Proc)

    def reset(self):
        self.__init__()


gv = GlobalVars()
