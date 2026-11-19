"""Run the baseline BP/AP derivations and explore AP's first I points."""

import sys
from collections import Counter

from framework import snapshot_paths
from framework.engine import FlowPaused, StepPoint
from framework.flow_run import CaptureInput, FlowRun, buffer_input_records
from framework.interpreter import fork_input, resume_checkpoint
from framework.snapshot import SnapshotError, deduplicate_outputs
from global_vars import MODEL_VERSION, gv
from origins.ap_init_origin import ApInitOrigin
from origins.bp_init_origin import BpInitOrigin


@buffer_input_records()
@deduplicate_outputs(pack_breaks=True)
def main() -> None:
    derive_line(BpInitOrigin)
    candidates = snapshot_paths.branch_outputs("bp", 0)
    anchors: dict[str, bytes] = {}

    def capture(point: str, execution: bytes) -> None:
        anchors[point] = execution

    pause = derive_line(ApInitOrigin, capture_input=capture)
    if pause is None:
        raise RuntimeError("the default AP derivation did not reach its break")
    try:
        resume_checkpoint(gv, pause.checkpoint, MODEL_VERSION, capture_input=capture)
    except FlowPaused as resumed:
        report_pause(resumed)
    if len(anchors) != 2:
        raise RuntimeError("AP must capture one I point at each entry")

    for point, execution in anchors.items():
        outcomes: Counter[str] = Counter()
        for candidate in candidates:
            print(f"{point} <- {candidate.stem}")
            try:
                fork_input(gv, point, execution, candidate, MODEL_VERSION)
            except FlowPaused as branch_pause:
                report_pause(branch_pause)
            outcomes[vars(gv.ap_init_flow)["state"].name] += 1
        summary = ", ".join(f"{state}={count}" for state, count in outcomes.items())
        print(f"{point}: {len(candidates)} branches ({summary})")


def report_pause(pause: FlowPaused) -> None:
    print(f"{pause.point}: {pause.state.name}\nCheckpoint: {pause.checkpoint}")


def derive_line(
    origin_type, *, capture_input: CaptureInput | None = None
) -> FlowPaused | None:
    gv.reset()
    origin = origin_type()
    try:
        with FlowRun(gv, origin.env, MODEL_VERSION, capture_input=capture_input):
            origin.start()
    except FlowPaused as pause:
        report_pause(pause)
        return pause
    except StepPoint as sp:
        print(f"{sp.point}: {sp.state}\n")
    return None


if __name__ == "__main__":
    if sys.argv[1:]:
        raise SystemExit("Usage: python3 main.py")
    try:
        main()
    except SnapshotError as error:
        raise SystemExit(str(error)) from error
