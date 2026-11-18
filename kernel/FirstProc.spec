Prop first: bool = True

Def check() -> ret:
    Actions:
        with GuardSmpAtomic(env.cv, self):
            ret = drive("self", "_check")

Def _check() -> ret:
    Actions:
        ret = self.first
        self.first = False
