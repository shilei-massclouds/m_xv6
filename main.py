"""Derive Entry"""

from framework.engine import StepPoint
from origins.ap_init_origin import ApInitOrigin
from origins.bp_init_origin import BpInitOrigin


def main() -> None:
    derive_line(BpInitOrigin())
    derive_line(ApInitOrigin())


def derive_line(origin) -> None:
    try:
        origin.start()
    except StepPoint as sp:
        print(f"{sp}\n")


if __name__ == "__main__":
    main()
