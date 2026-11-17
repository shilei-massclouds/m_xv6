"""Derive Entry"""

from framework.engine import StepPoint
from origins.bp_init_origin import BpInitOrigin


def main() -> None:
    origin = BpInitOrigin()
    try:
        origin.start()
    except StepPoint as sp:
        print(f"{sp}\n")


if __name__ == "__main__":
    main()
