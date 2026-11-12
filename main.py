"""Derive Entry"""

from argparse import ArgumentParser

from framework.snapshot import SnapshotError
from origins.ap_init_origin import ApInitOrigin
from origins.bp_init_origin import BpInitOrigin
from origins.initproc_origin import InitProcOrigin

ORIGINS = {
    "bp_init_origin": BpInitOrigin,
    "ap_init_origin": ApInitOrigin,
}


def main() -> None:
    parser = ArgumentParser(description="Run a model derivation.")
    parser.add_argument(
        "origin",
        nargs="?",
        choices=(*ORIGINS, "initproc_origin"),
        default="bp_init_origin",
    )
    parser.add_argument("--source", choices=("bp", "ap"))
    args = parser.parse_args()
    if args.origin == "initproc_origin":
        if args.source is None:
            parser.error("initproc_origin requires --source bp or --source ap")
        origin = InitProcOrigin(args.source)
    else:
        if args.source is not None:
            parser.error("--source is only accepted by initproc_origin")
        origin = ORIGINS[args.origin]()
    try:
        origin.start()
    except SnapshotError as exc:
        parser.exit(1, f"{parser.prog}: {exc}\n")


if __name__ == "__main__":
    main()
