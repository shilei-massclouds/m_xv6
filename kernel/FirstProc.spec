Prop first: bool = True

Def check():
    Actions:
        with GuardSmpAtomic(env.cv, self):
            return drive("self", "_check")

Def _check():
    Actions:
        ret = self.first
        self.first = False
        return ret
