"""Register the process line whose entry is provided by a CPU handoff."""

from dataclasses import dataclass

from framework.derivation import HandoffOrigin


@dataclass(frozen=True)
class ProcOrigin(HandoffOrigin):
    name: str = "proc"
