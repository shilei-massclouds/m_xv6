"""Derive Entry"""

from framework.engine import StepPoint
from framework.flow_run import FlowRun
from global_vars import MODEL_VERSION, gv
from origins.ap_init_origin import ApInitOrigin
from origins.bp_init_origin import BpInitOrigin


def main() -> None:
    derive_line(BpInitOrigin)
    derive_line(ApInitOrigin)


def derive_line(origin_type) -> None:
    gv.reset()
    origin = origin_type()
    try:
        with FlowRun(gv, origin.env, MODEL_VERSION):
            origin.start()
    except StepPoint as sp:
        print(f"{sp.point}: {sp.state}\n")


if __name__ == "__main__":
    main()
