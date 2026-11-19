Parent CpuFlow
Prop hartid: int = 1

Def _start():
    Requires:
        self.state is CpuFlowState.RUNNING
    Actions:
        drive("smp_started", "wait")
        self.state = CpuFlowState.RUNNING
        drive("io", "printk")
        drive("ap_hart", "_set_satp")
        drive("ap_hart", "_set_stvec")
        drive("plic", "_init_hart", hartid=self.hartid)
        LocalMultiTasks().enable(env.cv)
        drive("self", "schedule")
    Ensures:
        self.state is CpuFlowState.SWITCHED
        self.state is CpuFlowState.WFI
        self.state is CpuFlowState.BUSYCHECK
