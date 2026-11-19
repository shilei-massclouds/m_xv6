Prop signaled: bool = False

Def notify():
    Actions:
        with GuardSmpAtomic(env.cv, self):
            drive("self", "_notify")

Def _notify():
    Actions:
        assert not self.signaled
        self.signaled = True

Def wait():
    Actions:
        with GuardSmpAtomic(env.cv, self):
            drive("self", "_wait")

Def _wait():
    Requires:
        self.signaled
    Actions:
        pass
    Ensures:
        self.signaled

    Requires:
        not self.signaled
    Actions:
        break(CpuFlowState.BUSYCHECK, "self", "wait")
