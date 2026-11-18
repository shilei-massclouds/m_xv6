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
