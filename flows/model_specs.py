"""Register all flow models with stable names for cross-process snapshots."""

from pathlib import Path
from typing import Any

from framework.spec import SpecRegistry

_ROOT = Path(__file__).resolve().parents[1]
_MODEL_PATHS = (
    "flows/CpuFlow.spec",
    "flows/BpInitFlow.spec",
    "flows/ApInitFlow.spec",
    "flows/InitProcFlow.spec",
    "kernel/Page.spec",
    "kernel/PageAllocator.spec",
    "kernel/KernelPageTable.spec",
    "kernel/VM.spec",
    "kernel/PidGen.spec",
    "kernel/Proc.spec",
    "kernel/ProcTable.spec",
    "kernel/CurrentHart.spec",
    "kernel/Trap.spec",
    "kernel/IO.spec",
    "framework/BusyWait.spec",
    "kernel/SmpStarted.spec",
    "kernel/FirstProc.spec",
    "drivers/Console.spec",
    "drivers/Uart.spec",
    "drivers/DevSW.spec",
    "drivers/Plic.spec",
    "drivers/VirtIODisk.spec",
    "fs/BufferCache.spec",
    "fs/InodeTable.spec",
    "fs/FileTable.spec",
    "fs/FileSystem.spec",
    "structs/Array.spec",
)

registry = SpecRegistry([_ROOT / path for path in _MODEL_PATHS], module=__name__)


def new_model(name: str) -> Any:
    return registry.model(name)()
