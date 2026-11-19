"""Explore ordered combinations of independent spec flows."""

from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import partial
from itertools import product
from pathlib import Path
from typing import Protocol

from framework import snapshot_paths
from framework.engine import FlowPaused, TaskLocalEnv
from framework.flow_run import CaptureInput, FlowRun, buffer_input_records
from framework.interpreter import fork_input, resume_checkpoint
from framework.snapshot import deduplicate_outputs


class FlowOrigin(Protocol):
    env: TaskLocalEnv

    def start(self) -> None: ...


@dataclass
class _Line:
    # First the receiving flow, then each input flow in application order.
    flows: tuple[str, ...]
    inputs: dict[str, bytes] = field(default_factory=dict)


def _advance(
    step: Callable[[], object],
    root: object,
    model_version: int,
    capture_input: CaptureInput | None,
) -> str:
    resumed_sites: set[tuple[Path, int]] = set()
    while True:
        try:
            step()
        except FlowPaused as pause:
            print(f"{pause.point}: {pause.state.name}\nCheckpoint: {pause.checkpoint}")
            if pause.site in resumed_sites:
                return "paused"
            resumed_sites.add(pause.site)
            step = partial(
                resume_checkpoint,
                root,
                pause.checkpoint,
                model_version,
                capture_input=capture_input,
                capture_all_inputs=True,
            )
        else:
            return "completed"


def _start(
    origin: FlowOrigin,
    root: object,
    model_version: int,
    capture_input: CaptureInput | None,
) -> None:
    with FlowRun(
        root,
        origin.env,
        model_version,
        capture_input=capture_input,
        capture_all_inputs=True,
    ):
        origin.start()


def _combine(
    lines: list[_Line],
    source: str,
    candidates: list[Path],
    root: object,
    model_version: int,
    *,
    capture_next: bool,
) -> tuple[list[_Line], dict[tuple[str, ...], Counter[str]]]:
    results: list[_Line] = []
    summaries: dict[tuple[str, ...], Counter[str]] = {}
    for line in lines:
        if source in line.flows:
            continue
        flows = (*line.flows, source)
        outcomes = summaries.setdefault(flows, Counter())
        for (point, execution), candidate in product(line.inputs.items(), candidates):
            print(f"{point} <- {candidate.stem}")
            derived = _Line(flows)
            capture = derived.inputs.__setitem__ if capture_next else None
            step = partial(
                fork_input,
                root,
                point,
                execution,
                candidate,
                model_version,
                capture_input=capture,
                capture_all_inputs=True,
            )
            try:
                outcome = _advance(step, root, model_version, capture)
            except Exception as error:
                error.add_note(
                    f"Deriving from I point {point} with candidate {candidate}"
                )
                raise
            outcomes[outcome] += 1
            if derived.inputs:
                results.append(derived)
    return results, summaries


@buffer_input_records()
@deduplicate_outputs(pack_breaks=True)
def derive_flows(
    origins: Sequence[FlowOrigin],
    *,
    root: object,
    reset: Callable[[], None],
    model_version: int,
) -> None:
    """Apply each unused flow's first-order outputs to every I point of a line."""
    names = [origin.env.name for origin in origins]
    if len(set(names)) != len(names):
        raise ValueError("flow origins must have distinct names")

    lines: list[_Line] = []
    outputs: dict[str, list[Path]] = {}
    for origin, name in zip(origins, names):
        reset()
        line = _Line((name,))
        capture = line.inputs.__setitem__ if len(names) > 1 else None
        outcome = _advance(
            partial(_start, origin, root, model_version, capture),
            root,
            model_version,
            capture,
        )
        outputs[name] = snapshot_paths.branch_outputs(name, 0)
        lines.append(line)
        print(f"Order 1: {name}: {outcome}, {len(outputs[name])} O points")

    for order in range(2, len(names) + 1):
        next_lines: list[_Line] = []
        for source, candidates in outputs.items():
            results, summaries = _combine(
                lines,
                source,
                candidates,
                root,
                model_version,
                capture_next=order < len(names),
            )
            next_lines.extend(results)
            for flows, outcomes in summaries.items():
                chain = " -> ".join(reversed(flows))
                counts = ", ".join(f"{key}={count}" for key, count in outcomes.items())
                print(f"Order {order}: {chain}: {outcomes.total()} branches ({counts})")
        lines = next_lines
