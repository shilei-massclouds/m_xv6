Def _init():
    Actions:
        pass

Def putc_sync():
    Actions:
        with GuardLocalIrq(env.cv), GuardPreemption(env.cv):
            drive("self", "_putc_sync")

Def _putc_sync():
    ProtectedBy io
    Actions:
        pass
