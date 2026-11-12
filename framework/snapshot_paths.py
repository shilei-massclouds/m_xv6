"""Repository-local paths and checkpoint names used by boot origins and flows."""

from pathlib import Path

SNAPSHOT_DIR = Path(__file__).resolve().parents[1] / "snapshots"
BP_INITIALIZED_SNAPSHOT_PATH = SNAPSHOT_DIR / "bp_initialized.snap"
BP_BEFORE_SCHEDULE_SNAPSHOT_PATH = SNAPSHOT_DIR / "bp_before_schedule.snap"
AP_BEFORE_SCHEDULE_SNAPSHOT_PATH = SNAPSHOT_DIR / "ap_before_schedule.snap"

BP_INITIALIZED_CHECKPOINT = "bp_initialized"
BP_BEFORE_SCHEDULE_CHECKPOINT = "bp_before_schedule"
AP_BEFORE_SCHEDULE_CHECKPOINT = "ap_before_schedule"
