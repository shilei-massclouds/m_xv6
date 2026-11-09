"""BootInitTask Flow"""

from dataclasses import dataclass
from framework.engine import System


@dataclass
class BpInitFlow(System):
    def start(self, env: TaskLocalEnv, **kwargs):
        self.drive(env, "console", "_init")
        self.drive(env, "io", "_init")
        self.drive(env, "io", "printk")
        self.drive(env, "page_allocator", "_free_range")
        self.drive(env, "kernel_page_table", "_init")
