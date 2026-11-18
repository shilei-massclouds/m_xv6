Def _init():
    Actions:
        pass

Def printk(**kwargs):
    Actions:
        with GuardSpinLock(env.cv, self):
            # Keep the complete output operation indivisible.
            drive("console", "_sync_putc")
