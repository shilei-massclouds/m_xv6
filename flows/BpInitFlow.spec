Extends CpuFlow
Prop hartid: int = 0

Def _start():
    Requires:
        self.state is CpuFlowState.RUNNING
    Actions:
        drive("console", "_init")
        drive("io", "_init")
        drive("io", "printk")
        drive("page_allocator", "_free_range")
        drive("kernel_page_table", "_init")
        drive("bp_hart", "_set_satp")
        drive("proc_table", "_init")
        drive("trap", "_init")
        drive("bp_hart", "_set_stvec")
        drive("plic", "_init")
        drive("plic", "_init_hart", hartid=self.hartid)
        drive("bcache", "_init")
        drive("itable", "_init")
        drive("ftable", "_init")
        drive("vdisk", "_init")
        drive("self", "userinit")
        # BP cannot be preempted after notifying the other harts.
        Preemption().disable(env.cv)
        RemoteCpus().enable(env.cv)
        drive("smp_started", "notify")
        LocalMultiTasks().enable(env.cv)
        drive("self", "schedule")
    Ensures:
        self.state is CpuFlowState.SWITCHED
        self.state is CpuFlowState.WFI

Def userinit():
    Actions:
        drive("proc_table", "_alloc")
