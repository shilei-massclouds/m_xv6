Enum Privilege:
    USER = "user"
    KERNEL = "kernel"

Prop priv: Privilege = Privilege.KERNEL

Def start():
    Requires:
        self.priv is Privilege.KERNEL
    Actions:
        first = drive("first_proc", "check")
        drive("self", "first_init", first=first)
        # TODO: prepare_return.
        drive("self", "userret")
    Ensures:
        self.priv is Privilege.USER

Def first_init(first):
    Requires:
        first
    Actions:
        drive("self", "_first_init")
    Ensures:
        first

    Requires:
        not first
    Actions:
        pass
    Ensures:
        not first

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
