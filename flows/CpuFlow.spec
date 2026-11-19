Enum CpuFlowState:
    RUNNING
    WAITING
    BUSYCHECK
    SWITCHED
    WFI

Visibility CPUSCOPE_CV
Prop state: CpuFlowState = CpuFlowState.RUNNING
Prop last_selected_proc: ProcFlow | None = None

Def schedule():
    Actions:
        LocalIrq().enable(env.cv)
        LocalIrq().disable(env.cv)
        drive("self", "_schedule")

Def _schedule():
    Actions:
        next_runnable = drive("proc_table", "pick_runnable")
        drive("self", "_select_next", next_runnable=next_runnable)

Def _select_next(next_runnable):
    Requires:
        next_runnable is None
    Actions:
        self.state = CpuFlowState.WFI
    Ensures:
        self.state is CpuFlowState.WFI

    Requires:
        next_runnable is not None
    Actions:
        self.last_selected_proc = next_runnable
        env.prepare_handoff("proc", next_runnable, next_runnable.entry)
        break(CpuFlowState.SWITCHED, "self", "schedule")
    Ensures:
        self.state is CpuFlowState.SWITCHED
