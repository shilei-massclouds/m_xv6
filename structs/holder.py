"""Mutable holders for action output parameters."""

from dataclasses import dataclass


@dataclass
class Holder[T]:
    value: T | None = None
