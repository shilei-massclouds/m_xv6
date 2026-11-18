Enum Privilege:
    USER = "user"
    KERNEL = "kernel"

Prop priv: Privilege = Privilege.KERNEL

Def start():
    Requires:
        self.priv is Privilege.KERNEL
    Actions:
        first = drive("first_proc", "check")
        if first:
            drive("self", "_first_init")
        # TODO: prepare_return.
        drive("self", "userret")
    Ensures:
        self.priv is Privilege.USER

# TODO: record the current hart.
Def userret():
    Actions:
        self.priv = Privilege.USER

# Only for InitProc.
Def _first_init():
    Visibility TASKPRIVATE_CV
    Actions:
        drive("fs", "_init")
        drive("initproc", "_kexec")
