Prop table: Page = Page()

Def _init():
    Actions:
        self.table = drive("page_allocator", "_kalloc")
        # TODO: map multiple segments.
        drive("self", "_kvmmap")
        drive("proc_table", "_mapstacks")

Def _kvmmap():
    Actions:
        drive("vm", "_mappages", pt=self.table)
