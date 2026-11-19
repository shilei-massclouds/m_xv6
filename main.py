"""Explore the ordered combinations of the configured spec flows."""

import sys

from framework.derivation import derive_flows
from framework.snapshot import SnapshotError
from global_vars import MODEL_VERSION, gv
from origins.ap_init_origin import ApInitOrigin
from origins.bp_init_origin import BpInitOrigin


def main() -> None:
    derive_flows(
        [BpInitOrigin(), ApInitOrigin()],
        root=gv,
        reset=gv.reset,
        model_version=MODEL_VERSION,
    )


if __name__ == "__main__":
    if sys.argv[1:]:
        raise SystemExit("Usage: python3 main.py")
    try:
        main()
    except SnapshotError as error:
        raise SystemExit(
            "\n".join((str(error), *getattr(error, "__notes__", [])))
        ) from error
