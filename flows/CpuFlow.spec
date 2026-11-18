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
        drive("self", "select_next", next_runnable=next_runnable)

Def select_next(next_runnable):
    Requires:
        next_runnable is None
    Actions:
        self.state = CpuFlowState.WFI
    Ensures:
        self.state is CpuFlowState.WFI

    Requires:
        next_runnable is not None
    Actions:
        self.state = CpuFlowState.SWITCHED
    Ensures:
        self.state is CpuFlowState.SWITCHED
