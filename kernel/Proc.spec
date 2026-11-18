Enum ProcState:
    UNUSED = "unused"
    USED = "used"
    SLEEPING = "sleeping"
    RUNNABLE = "runnable"
    RUNNING = "running"
    ZOMBIE = "zombie"

Prop pid: int = 0
Prop state: ProcState = ProcState.UNUSED
Prop trampoline: Page = Page()
Prop trapframe: Page | None = None
Prop pagetable: Page | None = None

Def _kexec():
    Visibility TASKPRIVATE_CV
    Actions:
        pass
