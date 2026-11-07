"""Console Device"""

from dataclasses import dataclass, field

from framework.engine import System


@dataclass
class Console(System):
    def init(self, env: TaskLocalEnv):
        print("Console.init")
