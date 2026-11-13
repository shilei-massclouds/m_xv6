"""Branch contracts with entry selections local to each Python invocation."""

import sys
from dataclasses import dataclass
from types import FrameType
from typing import Literal

type Condition = bool
type Phase = Literal["requires", "ensures"]

# Entry predicates must be mutually exclusive. We reject multiple true branches
# in the current invocation, but boolean results cannot prove exclusivity for
# every possible input. TODO: implement a mechanism to detect that wider overlap.
_STATE_KEY = "__m_xv6_contract_state__"


@dataclass
class _ContractState:
    branch_index: int | None = 0
    branch_count: int = 1
    requires_called: bool = False
    ensures_called: bool = False


def _state(frame: FrameType) -> _ContractState:
    """Keep state in the caller's locals so it expires with that invocation."""
    state = frame.f_locals.get(_STATE_KEY)
    if state is None:
        state = _ContractState()
        frame.f_locals[_STATE_KEY] = state
    if not isinstance(state, _ContractState):
        raise TypeError(f"{_STATE_KEY} is reserved for contract state")
    return state


class ContractViolation(RuntimeError):
    """A condition failed at an explicit requires/ensures call site."""

    def __init__(
        self,
        phase: Phase,
        filename: str,
        lineno: int,
        function: str,
        *,
        branch_index: int | None = None,
    ):
        self.phase = phase
        self.filename = filename
        self.lineno = lineno
        self.function = function
        self.branch_index = branch_index
        branch = "" if branch_index is None else f" (entry branch {branch_index})"
        super().__init__(f"{phase} failed at {filename}:{lineno} in {function}{branch}")


def _validate(conditions: list[Condition], label: str) -> None:
    if not isinstance(conditions, list):
        raise TypeError(
            f"{label} expects a list of bool; got {type(conditions).__name__}"
        )
    for index, condition in enumerate(conditions):
        if not isinstance(condition, bool):
            raise TypeError(
                f"{label}[{index}] expects a bool; got {type(condition).__name__}"
            )


def _fail(phase: Phase, frame: FrameType, *, branch_index: int | None = None) -> None:
    raise ContractViolation(
        phase,
        frame.f_code.co_filename,
        frame.f_lineno,
        frame.f_code.co_qualname,
        branch_index=branch_index,
    )


def requires(conditions: list[Condition]) -> None:
    """Select one true entry branch; allow at most one call per invocation."""
    frame = sys._getframe(1)
    try:
        state = _state(frame)
        if state.requires_called:
            raise ValueError("requires may be called at most once per invocation")
        if state.ensures_called:
            raise ValueError("requires must precede ensures")
        state.requires_called = True
        state.branch_index = None
        _validate(conditions, "requires")
        state.branch_count = len(conditions)
        matches = [index for index, condition in enumerate(conditions) if condition]
        if not matches:
            _fail("requires", frame)
        if len(matches) > 1:
            raise ValueError(f"requires branches overlap: indices {matches}")
        state.branch_index = matches[0]
    finally:
        del frame


def ensures(branches: list[list[Condition]]) -> None:
    """Check OR within the entry's exit row; absent requires implies [True]."""
    frame = sys._getframe(1)
    try:
        state = _state(frame)
        if state.ensures_called:
            raise ValueError("ensures may be called at most once per invocation")
        state.ensures_called = True
        if not isinstance(branches, list):
            raise TypeError(
                f"ensures expects a list of lists of bool; got {type(branches).__name__}"
            )
        for index, conditions in enumerate(branches):
            _validate(conditions, f"ensures[{index}]")
        if len(branches) != state.branch_count:
            raise ValueError(
                "ensures row count must match requires branch count: "
                f"expected {state.branch_count}, got {len(branches)}"
            )
        if state.branch_index is None:
            raise ValueError("ensures needs a successful requires check")
        if not any(branches[state.branch_index]):
            _fail("ensures", frame, branch_index=state.branch_index)
    finally:
        del frame
