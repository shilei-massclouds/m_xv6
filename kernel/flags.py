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
