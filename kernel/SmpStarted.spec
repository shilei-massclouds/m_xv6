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
    Actions:
        if not self.signaled:
            exit_from("ap_xxx", "BUSY_CHECK")
