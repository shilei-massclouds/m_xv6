"""Context for Global vars"""

from dataclasses import dataclass, field
from os import PathLike

from drivers.console import Console
from drivers.devsw import DevSW
from drivers.plic import Plic
from drivers.uart import Uart
from drivers.virtio_disk import VirtIODisk
from flows.bp_init_flow import BpInitFlow
from framework.snapshot import SnapshotMetadata, load_snapshot, save_snapshot
from fs.buffer import BufferCache
from fs.file import FileTable
from fs.inode import InodeTable
from kernel.current import CurrentHart
from kernel.io import IO
from kernel.page_allocator import PageAllocator
from kernel.proc import PidGen, Proc, ProcTable, Scheduler
from kernel.trap import Trap
from kernel.vm import VM, KernelPageTable

MODEL_VERSION = 1


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
    scheduler: Scheduler = field(default_factory=Scheduler)
    current_hart: CurrentHart = field(default_factory=CurrentHart)
    trap: Trap = field(default_factory=Trap)
    plic: Plic = field(default_factory=Plic)
    bcache: BufferCache = field(default_factory=BufferCache)
    itable: InodeTable = field(default_factory=InodeTable)
    ftable: FileTable = field(default_factory=FileTable)
    vdisk: VirtIODisk = field(default_factory=VirtIODisk)
    initproc: Proc = field(default_factory=Proc)
    childproc: Proc = field(default_factory=Proc)
    pidgen: PidGen = field(default_factory=PidGen)

    def reset(self):
        self.__init__()

    def save_snapshot(
        self, path: str | PathLike[str], *, checkpoint: str
    ) -> SnapshotMetadata:
        return save_snapshot(
            self, path, checkpoint=checkpoint, model_version=MODEL_VERSION
        )

    def load_snapshot(
        self, path: str | PathLike[str], *, expected_checkpoint: str | None = None
    ) -> SnapshotMetadata:
        return load_snapshot(
            self,
            path,
            expected_checkpoint=expected_checkpoint,
            model_version=MODEL_VERSION,
        )


gv = GlobalVars()
