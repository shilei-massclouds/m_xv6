"""Initialize the first process and return to user mode."""

from dataclasses import dataclass, field
from enum import Enum

from framework.contention import TASKPRIVATE_CV
from framework.contracts import ensures, requires
from framework.engine import System, TaskLocalEnv, visibility


class Privilege(Enum):
    USER = "user"
    KERNEL = "kernel"


@dataclass
class InitProcFlow(System):
    priv: Privilege = field(default=Privilege.KERNEL, repr=False)

    def start(self, env: TaskLocalEnv, **kwargs):
        requires(self.priv is Privilege.KERNEL)

        first = self.drive(env, "first_proc", "check")
        if first:
            self.drive(env, "self", "_first_init")

        # Todo: prepare_return
        self.drive(env, "self", "userret")

        ensures(self.priv is Privilege.USER)

    # Todo: record current hart
    def userret(self, env: TaskLocalEnv, **kwargs):
        self.priv = Privilege.USER

    # Only for InitProc
    @visibility(TASKPRIVATE_CV)
    def _first_init(self, env: TaskLocalEnv, **kwargs):
        self.drive(env, "fs", "_init")
        self.drive(env, "initproc", "_kexec")
