Enum ProcState:
    UNUSED = "unused"
    USED = "used"
    SLEEPING = "sleeping"
    RUNNABLE = "runnable"
    RUNNING = "running"
    ZOMBIE = "zombie"

Enum Privilege:
    USER = "user"
    KERNEL = "kernel"

Prop pid: int = 0
Prop state: ProcState = ProcState.UNUSED
Prop priv: Privilege = Privilege.KERNEL
Prop entry: str = "start"
Prop trampoline: Page = Page()
Prop trapframe: Page | None = None
Prop pagetable: Page | None = None

Def start():
    Visibility TASKPRIVATE_CV
    Requires:
        self.state is ProcState.RUNNABLE and self.priv is Privilege.KERNEL
    Actions:
        self.state = ProcState.RUNNING
        first = drive("first_proc_flag", "check")
        drive("self", "_first_init", first=first)
        # TODO: prepare the return to user space.
        drive("self", "_userret")
    Ensures:
        self.state is ProcState.RUNNING and self.priv is Privilege.USER

Def switch_ret():
    Actions:
        pass

Def _first_init(first):
    Visibility TASKPRIVATE_CV
    Requires:
        first
    Actions:
        drive("fs", "_init")
        drive("self", "_kexec")

    Requires:
        not first
    Actions:
        pass

Def _userret():
    Visibility TASKPRIVATE_CV
    Actions:
        self.priv = Privilege.USER

Def _kexec():
    Visibility TASKPRIVATE_CV
    Actions:
        pass
