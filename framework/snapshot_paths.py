"""Repository-local paths for automatic action snapshots."""

from pathlib import Path

SNAPSHOT_DIR = Path(__file__).resolve().parents[1] / "snapshots"


def snapshot_path(flow: str, position: int, branch: int = 0) -> Path:
    """Name an action exit by flow, execution branch and position."""
    if not flow.isidentifier():
        raise ValueError("snapshot flow name must be a nonempty identifier")
    if type(position) is not int or position < 0:
        raise ValueError("snapshot position must be a nonnegative integer")
    if type(branch) is not int or branch < 0:
        raise ValueError("snapshot branch must be a nonnegative integer")
    return SNAPSHOT_DIR / f"{flow}_{branch}.{position}.snap"


# Legacy loading paths for inactive AP/InitProc origins; no longer generated.
BP_INITIALIZED_SNAPSHOT_PATH = SNAPSHOT_DIR / "bp_initialized.snap"
BP_BEFORE_SCHEDULE_SNAPSHOT_PATH = SNAPSHOT_DIR / "bp_before_schedule.snap"
AP_BEFORE_SCHEDULE_SNAPSHOT_PATH = SNAPSHOT_DIR / "ap_before_schedule.snap"

BP_INITIALIZED_CHECKPOINT = "bp_initialized"
BP_BEFORE_SCHEDULE_CHECKPOINT = "bp_before_schedule"
AP_BEFORE_SCHEDULE_CHECKPOINT = "ap_before_schedule"
