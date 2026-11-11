"""Explicit entry and exit checks with diagnostics at the caller's location."""

import sys
from collections.abc import Callable
from typing import Literal

type Condition = bool | Callable[[], bool]
type Phase = Literal["requires", "ensures"]


class ContractViolation(RuntimeError):
    """A condition failed at an explicit requires/ensures call site."""

    def __init__(self, phase: Phase, filename: str, lineno: int, function: str):
        self.phase = phase
        self.filename = filename
        self.lineno = lineno
        self.function = function
        super().__init__(f"{phase} failed at {filename}:{lineno} in {function}")


def _check(condition: Condition, phase: Phase) -> None:
    result = condition() if callable(condition) else condition
    if not isinstance(result, bool):
        raise TypeError(
            f"{phase} expects a bool or a callable returning bool; "
            f"got {type(result).__name__}"
        )
    if result:
        return
    # Stack: _check -> requires/ensures -> the explicit call site. Retain
    # location metadata only, rather than holding on to the caller's frame.
    frame = sys._getframe(2)
    try:
        location = frame.f_code.co_filename, frame.f_lineno, frame.f_code.co_qualname
    finally:
        del frame
    raise ContractViolation(phase, *location)


def requires(condition: Condition) -> None:
    """Check a bool or invoke a no-argument predicate once at method entry."""
    _check(condition, "requires")


def ensures(condition: Condition) -> None:
    """Check a bool or invoke a no-argument predicate once at a normal exit."""
    _check(condition, "ensures")
