"""Derive Entry"""

from argparse import ArgumentParser

from framework.snapshot import SnapshotError
from origins.ap_init_origin import ApInitOrigin
from origins.bp_init_origin import BpInitOrigin

ORIGINS = {
    "bp_init_origin": BpInitOrigin,
    "ap_init_origin": ApInitOrigin,
}


def main() -> None:
    parser = ArgumentParser(description="Run a boot initialization derivation.")
    parser.add_argument("origin", nargs="?", choices=ORIGINS, default="bp_init_origin")
    args = parser.parse_args()
    origin = ORIGINS[args.origin]()
    try:
        origin.start()
    except SnapshotError as exc:
        parser.exit(1, f"{parser.prog}: {exc}\n")


if __name__ == "__main__":
    main()
