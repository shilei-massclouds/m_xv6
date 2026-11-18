Enum CpuFlowState:
    RUNNING = "running"
    WAITING = "waiting"
    SWITCHED = "switched"
    WFI = "wfi"

Visibility CPUSCOPE_CV
Prop state: CpuFlowState = CpuFlowState.RUNNING

Def schedule():
    Actions:
        LocalIrq().enable(env.cv)
        LocalIrq().disable(env.cv)
        drive("self", "_schedule")

Def _schedule():
    Actions:
        next_runnable = drive("proc_table", "pick_runnable")
        if next_runnable is None:
            self.state = CpuFlowState.WFI
        else:
            self.state = CpuFlowState.SWITCHED
