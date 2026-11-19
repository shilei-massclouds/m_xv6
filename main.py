"""Derive Entry"""

from argparse import ArgumentParser
from pathlib import Path

from framework.engine import FlowPaused, StepPoint
from framework.flow_run import FlowRun
from framework.interpreter import resume_checkpoint
from framework.snapshot import SnapshotError
from global_vars import MODEL_VERSION, gv
from origins.ap_init_origin import ApInitOrigin
from origins.bp_init_origin import BpInitOrigin


def main() -> None:
    parser = ArgumentParser()
    parser.add_argument("--resume", type=Path, metavar="CHECKPOINT")
    args = parser.parse_args()
    try:
        if args.resume is not None:
            resume_checkpoint(gv, args.resume, MODEL_VERSION)
        else:
            derive_line(BpInitOrigin)
            derive_line(ApInitOrigin)
    except FlowPaused as pause:
        report_pause(pause)
    except SnapshotError as error:
        parser.exit(1, f"{error}\n")


def report_pause(pause: FlowPaused) -> None:
    print(f"{pause.point}: {pause.state.name}\nCheckpoint: {pause.checkpoint}")


def derive_line(origin_type) -> None:
    gv.reset()
    origin = origin_type()
    try:
        with FlowRun(gv, origin.env, MODEL_VERSION):
            origin.start()
    except FlowPaused as pause:
        report_pause(pause)
    except StepPoint as sp:
        print(f"{sp.point}: {sp.state}\n")


if __name__ == "__main__":
    main()
