"""Console Device"""

from dataclasses import dataclass, field

from framework.engine import System, TaskLocalEnv, protected_by
from structs.array import Array


@dataclass
class Console(System):
    buf: Array = field(default_factory=Array)
    r: int = field(default=0)
    w: int = field(default=0)
    e: int = field(default=0)

    def _init(self, env: TaskLocalEnv):
        self.drive(env, "uart", "_init")
        self.drive(
            env, "devsw", "_register", dev="CONSOLE", read=self._read, write=self._write
        )

    def _read(self, env: TaskLocalEnv):
        pass

    def _write(self, env: TaskLocalEnv):
        pass

    @protected_by("io")
    def _sync_putc(self, env: TaskLocalEnv):
        self.drive(env, "uart", "putc_sync")
