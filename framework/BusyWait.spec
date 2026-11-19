Prop signaled: bool = False

Def notify():
    Actions:
        with GuardSmpAtomic(env.cv, self):
            drive("self", "_notify")

Def _notify():
    Actions:
        assert not self.signaled
        self.signaled = True
        env.notify_busy_wait(self)

Def wait():
    Actions:
        with GuardSmpAtomic(env.cv, self):
            drive("self", "_wait")

Def _wait():
    Requires:
        self.signaled
    Actions:
        env.observe_busy_wait(self)
    Ensures:
        self.signaled

    Requires:
        not self.signaled
    Actions:
        break(CpuFlowState.BUSYCHECK, "self", "wait")
