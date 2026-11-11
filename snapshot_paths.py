"""Repository-local paths and checkpoint names used by boot origins and flows."""

from pathlib import Path

SNAPSHOT_DIR = Path(__file__).resolve().parent / "snapshots"
BP_INITIALIZED_SNAPSHOT_PATH = SNAPSHOT_DIR / "bp_initialized.snap"

BP_INITIALIZED_CHECKPOINT = "bp_initialized"
