Def _free_range():
    Actions:
        # TODO: free each page in the range.
        drive("self", "kfree")

Def kfree():
    Actions:
        with GuardSpinLock(env.cv, self):
            drive("self", "_kfree")

Def _kfree():
    Actions:
        pass

Def _kalloc() -> page:
    Actions:
        page = Page()
