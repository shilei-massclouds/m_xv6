Prop buf: Array = Array()
Prop r: int = 0
Prop w: int = 0
Prop e: int = 0

Def _init():
    Actions:
        drive("uart", "_init")
        drive("devsw", "_register", dev="CONSOLE", read=self._read, write=self._write)

Def _read():
    Actions:
        pass

Def _write():
    Actions:
        pass

Def _sync_putc():
    ProtectedBy io
    Actions:
        drive("uart", "putc_sync")
