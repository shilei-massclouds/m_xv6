"""Derive Entry"""

from origins.bp_init_origin import BpInitOrigin


def main() -> None:
    origin = BpInitOrigin()
    origin.start()


if __name__ == "__main__":
    main()
