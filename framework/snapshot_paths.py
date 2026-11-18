"""Repository-local paths for spec operation inputs and outputs."""

from pathlib import Path

SNAPSHOT_DIR = Path(__file__).resolve().parents[1] / "snapshots"


def flow_directory(flow: str) -> Path:
    if not flow.isidentifier():
        raise ValueError("snapshot flow name must be a nonempty identifier")
    return SNAPSHOT_DIR / flow


def input_records_path(flow: str) -> Path:
    return flow_directory(flow) / "inputs" / "records.json"


def snapshot_path(flow: str, position: int, branch: int = 0) -> Path:
    """Name an output by flow, execution branch and completion position."""
    directory = flow_directory(flow)
    if type(position) is not int or position < 0:
        raise ValueError("snapshot position must be a nonnegative integer")
    if type(branch) is not int or branch < 0:
        raise ValueError("snapshot branch must be a nonnegative integer")
    return directory / "outputs" / f"{flow}_{branch}.{position}.snap"
