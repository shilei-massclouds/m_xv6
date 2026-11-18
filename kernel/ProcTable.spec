Prop table: list[Proc]

Def _pick_runnable() -> nextp:
    Requires:
        (not self.table)
    Actions:
        nextp = None
    Ensures:
        (not self.table) and nextp is None

    Requires:
        len(self.table) >= 1
    Actions:
        nextp = self.table.pop(0)
    Ensures:
        (not self.table) and nextp is not None and nextp not in self.table
        len(self.table) >= 1 and nextp is not None and nextp not in self.table


Def _init():
    Actions:
        pass

Def _mapstacks():
    Actions:
        # TODO: allocate stacks according to the process count.
        drive("page_allocator", "_kalloc")
        drive("kernel_page_table", "_kvmmap")

Def _alloc():
    Actions:
        # TODO: check that _mapstacks has been called.
        proc = Proc()
        assert proc.state is ProcState.UNUSED, "process must be unused before initialization"
        proc.pid = drive("pidgen", "_alloc")
        proc.state = ProcState.USED
        proc.trapframe = drive("page_allocator", "_kalloc")
        proc.pagetable = drive("page_allocator", "_kalloc")
        drive("vm", "_mappages", pt=proc.trampoline)
        drive("vm", "_mappages", pt=proc.pagetable)
        # TODO: initialize cwd.
        proc.state = ProcState.RUNNABLE
        drive("self", "put_runnable", task=proc)
        if gv.initproc is None:
            gv.initproc = proc

Def pick_runnable(**kwargs) -> nextp:
    Actions:
        with GuardSpinLock(env.cv, self):
            nextp = drive("self", "_pick_runnable")
        # The Python AP scheduler still uses a Holder output parameter.
        if "ret" in kwargs:
            kwargs["ret"].value = nextp

Def put_runnable(**kwargs):
    Actions:
        with GuardSpinLock(env.cv, self):
            drive("self", "_put_runnable", **kwargs)

Def _put_runnable(**kwargs):
    Requires:
        not self.table
    Actions:
        self.table.append(kwargs["task"])
    Ensures:
        len(self.table) == 1

    Requires:
        len(self.table) >= 1
    Actions:
        self.table.append(kwargs["task"])
    Ensures:
        len(self.table) > 1
