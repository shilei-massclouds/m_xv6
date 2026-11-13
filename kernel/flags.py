"""Current Hart"""

from dataclasses import dataclass

from framework.engine import System, TaskLocalEnv
from framework.sync_primitives import GuardSmpAtomic


@dataclass
class SmpStarted(System):
    started: bool = False

    def notify(self, env: TaskLocalEnv):
        with GuardSmpAtomic(env.cv, self):
            self.drive(env, "self", "_notify")

    def _notify(self, env: TaskLocalEnv):
        self.started = True

    def wait(self, env: TaskLocalEnv):
        with GuardSmpAtomic(env.cv, self):
            self.drive(env, "self", "_wait")

    def _wait(self, env: TaskLocalEnv):
        assert self.started


@dataclass
class FirstProc(System):
    first: bool = True

    def check(self, env: TaskLocalEnv) -> bool:
        with GuardSmpAtomic(env.cv, self):
            return self.drive(env, "self", "_check")

    def _check(self, env: TaskLocalEnv) -> bool:
        ret = self.first
        self.first = False
        return ret
